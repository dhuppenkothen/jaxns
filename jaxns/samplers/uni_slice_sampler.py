from typing import TypeVar, NamedTuple, Tuple

import jax
from jax import numpy as jnp, random, lax

from jaxns.framework.bases import BaseAbstractModel
from jaxns.internals.cumulative_ops import cumulative_op_static
from jaxns.internals.types import PRNGKey, FloatArray, BoolArray, Sample, float_type, int_type, \
    StaticStandardNestedSamplerState, \
    IntArray, UType, StaticStandardSampleCollection
from jaxns.samplers.abc import SamplerState
from jaxns.samplers.bases import SeedPoint, BaseAbstractMarkovSampler

__all__ = [
    'UniDimSliceSampler'
]

T = TypeVar('T')


def _sample_direction(n_key: PRNGKey, ndim: int) -> FloatArray:
    """
    Choose a direction randomly from S^(D-1).

    Args:
        n_key: PRNG key
        ndim: int, number of dimentions

    Returns:
        direction: [D] direction from S^(D-1)
    """
    if ndim == 1:
        return jnp.ones(())
    direction = random.normal(n_key, shape=(ndim,), dtype=float_type)
    direction /= jnp.linalg.norm(direction)
    return direction


def _slice_bounds(point_U0: FloatArray, direction: FloatArray) -> Tuple[FloatArray, FloatArray]:
    """
    Compute the slice bounds, t, where point_U0 + direction * t intersects uit cube boundary.

    Args:
        point_U0: [D]
        direction: [D]

    Returns:
        left_bound: left most point (<= 0).
        right_bound: right most point (>= 0).
    """
    t1 = (1. - point_U0) / direction
    t1_right = jnp.min(jnp.where(t1 >= 0., t1, jnp.inf))
    t1_left = jnp.max(jnp.where(t1 <= 0., t1, -jnp.inf))
    t0 = -point_U0 / direction
    t0_right = jnp.min(jnp.where(t0 >= 0., t0, jnp.inf))
    t0_left = jnp.max(jnp.where(t0 <= 0., t0, -jnp.inf))
    right_bound = jnp.minimum(t0_right, t1_right)
    left_bound = jnp.maximum(t0_left, t1_left)
    return left_bound, right_bound


def _pick_point_in_interval(key: PRNGKey, point_U0: FloatArray, direction: FloatArray, left: FloatArray,
                            right: FloatArray) -> Tuple[FloatArray, FloatArray]:
    """
    Select a point along slice in [point_U0 + direction * left, point_U0 + direction * right]

    Args:
        key: PRNG key
        point_U0: [D]
        direction: [D]
        left: left most point (<= 0).
        right: right most point (>= 0).

    Returns:
        point_U: [D]
        t: selection point between [left, right]
    """
    t = random.uniform(key, minval=left, maxval=right, dtype=float_type)
    point_U = point_U0 + t * direction
    # close_to_zero = (left >= -10*jnp.finfo(left.dtype).eps) & (right <= 10*jnp.finfo(right.dtype).eps)
    # point_U = jnp.where(close_to_zero, point_U0, point_U)
    # t = jnp.where(close_to_zero, jnp.zeros_like(t), t)
    return point_U, t


def _shrink_interval(key: PRNGKey, t: FloatArray, left: FloatArray, right: FloatArray, log_L_proposal: FloatArray,
                     log_L_constraint: FloatArray, log_L0: FloatArray,
                     midpoint_shrink: bool) -> Tuple[FloatArray, FloatArray]:
    """
    Not successful proposal, so shrink, optionally apply exponential shrinkage.
    """
    # witout exponential shrinkage, we shrink to failed proposal point, which is 100% correct.
    left = jnp.where(t < 0., t, left)
    right = jnp.where(t > 0., t, right)

    if midpoint_shrink:
        # For this to be correct it must be invariant to monotonic rescaling of the likelihood.
        # Therefore, it must only use the knowledge of ordering of the likelihoods.
        # Basic version: shrink to midpoint of interval, i.e. alpha = 0.5.
        # Extended version: shrink to random point in interval.
        alpha = random.uniform(key)
        left = jnp.where((t < 0.), alpha * left, left)
        right = jnp.where((t > 0.), alpha * right, right)
    return left, right


def _new_proposal(key: PRNGKey, seed_point: SeedPoint, midpoint_shrink: bool, perfect: bool,
                  gradient_slice: bool,
                  log_L_constraint: FloatArray,
                  model: BaseAbstractModel) -> Tuple[FloatArray, FloatArray, IntArray]:
    """
    Sample from a slice about a seed point.

    Args:
        key: PRNG key
        seed_point: the seed point to sample from
        midpoint_shrink: if true then contract to the midpoint of interval on rejection. Otherwise, normal contract
        perfect: if true then perform exponential shrinkage from maximal bounds, requiring no step-out procedure.
        gradient_slice: if true the slice along gradient direction
        log_L_constraint: the constraint to sample within
        model: the model to sample from

    Returns:
        point_U: the new sample
        log_L: the log-likelihood of the new sample
        num_likelihood_evaluations: the number of likelihood evaluations performed
    """

    class Carry(NamedTuple):
        key: PRNGKey
        direction: FloatArray
        left: FloatArray
        right: FloatArray
        t: FloatArray
        point_U: UType
        log_L: FloatArray
        num_likelihood_evaluations: IntArray

    def cond(carry: Carry) -> BoolArray:
        close_to_zero_interval = (carry.right - carry.left) <= 2 * jnp.finfo(carry.right.dtype).eps
        satisfaction = carry.log_L > log_L_constraint
        # Allow if on plateau to fly around the plateau for a while
        lesser_satisfaction = jnp.bitwise_and(seed_point.log_L0 == log_L_constraint, carry.log_L == log_L_constraint)
        done = jnp.bitwise_or(jnp.bitwise_or(close_to_zero_interval, satisfaction), lesser_satisfaction)
        return jnp.bitwise_not(done)

    def body(carry: Carry) -> Carry:
        key, t_key, shrink_key = random.split(carry.key, 3)
        left, right = _shrink_interval(
            key=shrink_key,
            t=carry.t,
            left=carry.left,
            right=carry.right,
            log_L_proposal=carry.log_L,
            log_L_constraint=log_L_constraint,
            log_L0=seed_point.log_L0,
            midpoint_shrink=midpoint_shrink
        )
        point_U, t = _pick_point_in_interval(
            key=t_key,
            point_U0=seed_point.U0,
            direction=carry.direction,
            left=left,
            right=right
        )
        log_L = model.forward(point_U)
        num_likelihood_evaluations = carry.num_likelihood_evaluations + jnp.ones_like(carry.num_likelihood_evaluations)
        return Carry(
            key=key,
            t=t,
            left=left,
            right=right,
            point_U=point_U,
            log_L=log_L,
            num_likelihood_evaluations=num_likelihood_evaluations,
            direction=carry.direction
        )

    key, n_key, t_key = random.split(key, 3)
    if gradient_slice:
        direction = jax.grad(model.forward)(seed_point.U0)
        norm = jnp.linalg.norm(direction)
        direction /= norm
        direction = jnp.where(
            jnp.bitwise_or(jnp.equal(norm, jnp.zeros_like(norm)), ~jnp.isfinite(norm)),
            _sample_direction(n_key, seed_point.U0.size),
            direction
        )
        num_likelihood_evaluations = jnp.full((), 1, int_type)
        (left, right) = _slice_bounds(
            point_U0=seed_point.U0,
            direction=direction
        )
        left = jnp.zeros_like(left)
    else:
        direction = _sample_direction(n_key, seed_point.U0.size)
        num_likelihood_evaluations = jnp.full((), 0, int_type)
        if perfect:
            (left, right) = _slice_bounds(
                point_U0=seed_point.U0,
                direction=direction
            )
        else:
            # TODO: implement doubling step out
            raise NotImplementedError("TODO: implement doubling step out")
    point_U, t = _pick_point_in_interval(
        key=t_key,
        point_U0=seed_point.U0,
        direction=direction,
        left=left,
        right=right
    )
    log_L = model.forward(point_U)
    init_carry = Carry(
        key=key,
        direction=direction,
        left=left,
        right=right,
        t=t,
        point_U=point_U,
        log_L=log_L,
        num_likelihood_evaluations=num_likelihood_evaluations
    )

    carry = lax.while_loop(
        cond_fun=cond,
        body_fun=body,
        init_val=init_carry
    )
    return carry.point_U, carry.log_L, carry.num_likelihood_evaluations


class UniDimSliceSampler(BaseAbstractMarkovSampler):
    """
    Slice sampler for a single dimension. Produces correlated (non-i.i.d.) samples.
    """

    def __init__(self, model: BaseAbstractModel, num_slices: int, num_phantom_save: int, midpoint_shrink: bool,
                 perfect: bool, gradient_slice: bool = False):
        """
        Unidimensional slice sampler.

        Args:
            model: AbstractModel
            num_slices: number of slices between acceptance. Note: some other software use units of prior dimension.
            midpoint_shrink: if true then contract to the midpoint of interval on rejection. Otherwise, contract to
                rejection point. Speeds up convergence, but introduces minor auto-correlation.
            num_phantom_save: number of phantom samples to save. Phantom samples are samples that meeting the constraint
                but are not accepted. They can be used for numerous things, e.g. to estimate the evidence uncertainty.
            perfect: if true then perform exponential shrinkage from maximal bounds, requiring no step-out procedure.
                Otherwise, uses a doubling procedure (exponentially finding bracket).
                Note: Perfect is a misnomer, as perfection also depends on the number of slices between acceptance.
            gradient_slice: if true then always slice along gradient direction.
        """
        super().__init__(model=model)
        if num_slices < 1:
            raise ValueError(f"num_slices should be >= 1, got {num_slices}.")
        if num_phantom_save < 0:
            raise ValueError(f"num_phantom_save should be >= 0, got {num_phantom_save}.")
        if num_phantom_save >= num_slices:
            raise ValueError(f"num_phantom_save should be < num_slices, got {num_phantom_save} >= {num_slices}.")
        self.num_slices = int(num_slices)
        self.num_phantom_save = int(num_phantom_save)
        self.midpoint_shrink = bool(midpoint_shrink)
        self.perfect = bool(perfect)
        self.gradient_slice = bool(gradient_slice)
        if not self.perfect:
            raise ValueError("Only perfect slice sampler is implemented.")

    def num_phantom(self) -> int:
        return self.num_phantom_save

    def pre_process(self, state: StaticStandardNestedSamplerState) -> SamplerState:
        sample_collection = jax.tree.map(lambda x: x[state.front_idx], state.sample_collection)
        if self.perfect:  # nothing needed
            return (sample_collection,)
        else:  # TODO: step out with doubling, using ellipsoidal clustering
            return (sample_collection,)  # multi_ellipsoidal_params()

    def post_process(self, sample_collection: StaticStandardSampleCollection,
                     sampler_state: SamplerState) -> SamplerState:
        if self.perfect:  # nothing needed
            return (sample_collection,)
        else:  # TODO: step out with doubling, using ellipsoidal clustering, could shrink ellipsoids
            return (sample_collection,)

    def get_seed_point(self, key: PRNGKey, sampler_state: SamplerState,
                       log_L_constraint: FloatArray) -> SeedPoint:

        sample_collection: StaticStandardSampleCollection
        (sample_collection,) = sampler_state

        select_mask = sample_collection.log_L > log_L_constraint
        # If non satisfied samples, then choose randomly from them.
        any_satisfied = jnp.any(select_mask)
        yes_ = jnp.asarray(0., float_type)
        no_ = jnp.asarray(-jnp.inf, float_type)
        unnorm_select_log_prob = jnp.where(
            any_satisfied,
            jnp.where(select_mask, yes_, no_),
            yes_
        )
        # Choose randomly where mask is True
        g = random.gumbel(key, shape=unnorm_select_log_prob.shape)
        sample_idx = jnp.argmax(g + unnorm_select_log_prob)

        return SeedPoint(
            U0=sample_collection.U_samples[sample_idx],
            log_L0=sample_collection.log_L[sample_idx]
        )

    def get_sample_from_seed(self, key: PRNGKey, seed_point: SeedPoint, log_L_constraint: FloatArray,
                             sampler_state: SamplerState) -> Tuple[Sample, Sample]:

        def propose_op(sample: Sample, key: PRNGKey) -> Sample:
            U_sample, log_L, num_likelihood_evaluations = _new_proposal(
                key=key,
                seed_point=SeedPoint(
                    U0=sample.U_sample,
                    log_L0=sample.log_L
                ),
                midpoint_shrink=self.midpoint_shrink,
                perfect=self.perfect,
                gradient_slice=self.gradient_slice,
                log_L_constraint=log_L_constraint,
                model=self.model
            )
            return Sample(
                U_sample=U_sample,
                log_L_constraint=log_L_constraint,
                log_L=log_L,
                num_likelihood_evaluations=num_likelihood_evaluations + sample.num_likelihood_evaluations
            )

        init_sample = Sample(
            U_sample=seed_point.U0,
            log_L_constraint=log_L_constraint,
            log_L=seed_point.log_L0,
            num_likelihood_evaluations=jnp.asarray(0, int_type)
        )
        final_sample, cumulative_samples = cumulative_op_static(
            op=propose_op,
            init=init_sample,
            xs=random.split(key, self.num_slices),
            unroll=2
        )

        # Last sample is the final sample, the rest are potential phantom samples
        # Take only the last num_phantom_save phantom samples
        phantom_samples: Sample = jax.tree.map(lambda x: x[-(self.num_phantom_save + 1):-1], cumulative_samples)

        # Due to the cumulative nature of the sampler, the final number of likelihood evaluations should be divided
        # equally among the accepted sample and retained phantom samples.
        num_likelihood_evaluations_per_phantom_sample = (
                final_sample.num_likelihood_evaluations / (self.num_phantom_save + 1)
        ).astype(int_type)
        num_likelihood_evaluations_per_accepted_sample = (
                final_sample.num_likelihood_evaluations - num_likelihood_evaluations_per_phantom_sample * self.num_phantom_save
        )
        final_sample = final_sample._replace(
            num_likelihood_evaluations=num_likelihood_evaluations_per_accepted_sample
        )
        phantom_samples = phantom_samples._replace(
            num_likelihood_evaluations=jnp.full(
                phantom_samples.num_likelihood_evaluations.shape,
                num_likelihood_evaluations_per_phantom_sample,
                phantom_samples.num_likelihood_evaluations.dtype
            )
        )
        return final_sample, phantom_samples

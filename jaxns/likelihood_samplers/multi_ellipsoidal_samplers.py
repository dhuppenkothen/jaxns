from typing import NamedTuple

from jaxns.types import BoolArray, IntArray
from jaxns.types import PRNGKey, FloatArray
from jax import random, numpy as jnp
from jax.lax import while_loop

from jaxns.likelihood_samplers.multi_ellipsoid.multi_ellipsoid_utils import ellipsoid_clustering, MultEllipsoidState
from jaxns.likelihood_samplers.multi_ellipsoid.multi_ellipsoid_utils import sample_multi_ellipsoid
from jaxns.abc import AbstractRejectionSampler
from jaxns.statistics import compute_num_live_points_from_unit_threads, compute_evidence_no_stats
from jaxns.types import NestedSamplerState, LivePoints, Sample, int_type

__all__ = ['MultiellipsoidalSampler']


class MultiellipsoidalSampler(AbstractRejectionSampler):
    def __init__(self, depth: int, *args, **kwargs):
        self._depth = depth
        super().__init__(*args, **kwargs)

    @property
    def max_num_ellipsoids(self):
        return 2 ** self._depth

    def preprocess(self, state: NestedSamplerState, live_points: LivePoints) -> MultEllipsoidState:
        key, sampler_key = random.split(state.key)

        num_live_points = compute_num_live_points_from_unit_threads(
            log_L_constraints=state.sample_collection.reservoir.log_L_constraint,
            log_L_samples=state.sample_collection.reservoir.log_L,
            num_samples=state.sample_collection.sample_idx,
            sorted_collection=True
        )
        evidence_calculation = compute_evidence_no_stats(sample_collection=state.sample_collection,
                                                         num_live_points=num_live_points)

        return ellipsoid_clustering(
            key=sampler_key,
            points=live_points.reservoir.point_U,
            log_VS=evidence_calculation.log_X_mean,
            max_num_ellipsoids=self.max_num_ellipsoids,
            method='em_gmm'
        )

    def get_sample(self, key: PRNGKey, log_L_constraint: FloatArray, live_points: LivePoints,
                   preprocess_data: MultEllipsoidState) -> Sample:
        def _sample_multi_ellipsoid(key: PRNGKey) -> FloatArray:
            _, U = sample_multi_ellipsoid(
                key=key,
                mu=preprocess_data.params.mu,
                radii=preprocess_data.params.radii,
                rotation=preprocess_data.params.rotation,
                unit_cube_constraint=True
            )
            return U

        class CarryState(NamedTuple):
            done: BoolArray
            key: PRNGKey
            U: FloatArray
            log_L: FloatArray
            log_L_constraint: FloatArray
            num_likelihood_evals: IntArray

        def body(carry_state: CarryState):
            key, sample_key = random.split(carry_state.key, 2)
            log_L = self.model.forward(U=carry_state.U)
            num_likelihood_evals = carry_state.num_likelihood_evals + jnp.asarray(1, int_type)
            # backoff by one e-fold per attempt after efficiency threshold reached
            log_L_constraint = jnp.where(num_likelihood_evals > 1. / self.efficiency_threshold,
                                         carry_state.log_L_constraint - 0.1, carry_state.log_L_constraint)
            done = log_L > log_L_constraint
            U = jnp.where(done, carry_state.U, _sample_multi_ellipsoid(key=sample_key))
            return CarryState(done=done, key=key, U=U, log_L=log_L, num_likelihood_evals=num_likelihood_evals,
                              log_L_constraint=log_L_constraint)

        key, sample_key = random.split(key, 2)
        init_carry_state = CarryState(done=jnp.asarray(False),
                                      key=key,
                                      U=_sample_multi_ellipsoid(key=sample_key),
                                      log_L=log_L_constraint,
                                      log_L_constraint=log_L_constraint,
                                      num_likelihood_evals=jnp.asarray(0, int_type))

        carry_state = while_loop(lambda s: jnp.bitwise_not(s.done), body, init_carry_state)

        sample = Sample(point_U=carry_state.U,
                        log_L_constraint=carry_state.log_L_constraint,
                        log_L=carry_state.log_L,
                        num_likelihood_evaluations=carry_state.num_likelihood_evals,
                        num_slices=jnp.asarray(0, int_type),
                        iid=jnp.asarray(True, jnp.bool_))
        return sample
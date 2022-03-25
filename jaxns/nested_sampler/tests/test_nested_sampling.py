import numpy as np
from jax import numpy as jnp, random, jit
from jax._src.scipy.linalg import solve_triangular
from jax._src.scipy.special import gammaln

from jaxns import NestedSampler, resample, summary
from jaxns.utils import evidence_posterior_samples
from jaxns.prior_transforms import PriorChain, UniformPrior, MVNPrior, GammaPrior


def test_nested_sampling_basic():
    from jaxns.plotting import plot_diagnostics
    def log_likelihood(x):
        return - jnp.sum(x**2)

    with PriorChain() as prior_chain:
        UniformPrior('x', 0., 1.)

    ns = NestedSampler(log_likelihood, prior_chain)
    results = ns(key=random.PRNGKey(43), num_live_points=100, termination_live_evidence_frac=1e-2)
    plot_diagnostics(results)
    summary(results)

    log_Z_samples = evidence_posterior_samples(random.PRNGKey(42),
                                               results.num_live_points_per_sample[:results.total_num_samples],
                                               results.log_L_samples[:results.total_num_samples], S=1000)
    assert jnp.isclose(results.log_Z_mean, jnp.mean(log_Z_samples), atol=1e-3)
    assert jnp.isclose(results.log_Z_uncert,jnp.std(log_Z_samples), atol=1e-3)

    assert jnp.bitwise_not(jnp.isnan(results.log_Z_mean))
    assert jnp.isclose(results.log_Z_mean, -1./3., atol=1.75*results.log_Z_uncert)



def test_nested_sampling_max_likelihood():
    def log_likelihood(x):
        return -0.5*jnp.sum(x**4 - 16 * x**2 + 5 * x)

    def test_example(ndim):
        with PriorChain() as prior_chain:
            UniformPrior('x', -5.*jnp.ones(ndim), 5.*jnp.ones(ndim))

        ns = NestedSampler(log_likelihood, prior_chain,
                           sampler_kwargs=dict(num_slices=prior_chain.U_ndims*5))
        results = ns(key=random.PRNGKey(42),
                     termination_max_num_steps=30,
                     maximise_likelihood=True)
        lower_bound = 39.16616*ndim
        upper_bound = 39.16617*ndim
        x_max = -2.903534
        # print(ndim,jnp.abs(results.sample_L_max['x']-x_max))
        # print(ndim,jnp.abs(results.log_L_max-0.5*(lower_bound+upper_bound)))
        assert jnp.allclose(results.sample_L_max['x'], x_max, atol=9e-3*ndim/9)
        assert jnp.isclose(results.log_L_max,
                           0.5*(lower_bound+upper_bound),
                           atol=2.*(upper_bound-lower_bound))

    for ndim in range(2,10):
        test_example(ndim)


def test_nested_sampling_basic_parallel():

    import os
    os.environ["XLA_FLAGS"] = "--xla_force_host_platform_device_count=2"

    def log_likelihood(x):
        return - jnp.sum(x**2)

    with PriorChain() as prior_chain:
        UniformPrior('x', 0., 1.)

    ns = NestedSampler(log_likelihood, prior_chain, num_parallel_samplers=2)
    results = ns(key=random.PRNGKey(42))

    ns_serial = NestedSampler(log_likelihood, prior_chain)
    results_serial = ns_serial(key=random.PRNGKey(42))
    assert jnp.isclose(results_serial.log_Z_mean, results.log_Z_mean)


def test_nested_sampling_mvn():
    from jaxns import summary
    def log_normal(x, mean, cov):
        L = jnp.linalg.cholesky(cov)
        dx = x - mean
        dx = solve_triangular(L, dx, lower=True)
        return -0.5 * x.size * jnp.log(2. * jnp.pi) - jnp.sum(jnp.log(jnp.diag(L))) \
               - 0.5 * dx @ dx

    ndims = 4
    prior_mu = 2 * jnp.ones(ndims)
    prior_cov = jnp.diag(jnp.ones(ndims)) ** 2

    data_mu = jnp.zeros(ndims)
    data_cov = jnp.diag(jnp.ones(ndims)) ** 2
    data_cov = jnp.where(data_cov == 0., 0.95, data_cov)

    true_logZ = log_normal(data_mu, prior_mu, prior_cov + data_cov)
    # not super happy with this being 1.58 and being off by like 0.1. Probably related to the ESS.
    post_mu = prior_cov @ jnp.linalg.inv(prior_cov + data_cov) @ data_mu + data_cov @ jnp.linalg.inv(
        prior_cov + data_cov) @ prior_mu

    log_likelihood = lambda x, **kwargs: log_normal(x, data_mu, data_cov)

    with PriorChain() as prior_chain:
        MVNPrior('x', prior_mu, prior_cov)

    ns = NestedSampler(log_likelihood, prior_chain)
    results = ns(key=random.PRNGKey(42))
    summary(results)
    assert jnp.isclose(results.log_Z_mean, true_logZ, atol=1.75 * results.log_Z_uncert)

    # fails with gradient_boost=False
    ns = NestedSampler(log_likelihood, prior_chain, sampler_kwargs=dict(gradient_boost=True))
    results = ns(key=random.PRNGKey(43))
    summary(results)
    assert jnp.isclose(results.log_Z_mean,  true_logZ, atol=1.75 * results.log_Z_uncert)




def test_nested_sampling_dynamic():

    from jaxns.plotting import plot_diagnostics, plot_cornerplot
    from jaxns.utils import summary
    def log_normal(x, mean, cov):
        L = jnp.linalg.cholesky(cov)
        dx = x - mean
        dx = solve_triangular(L, dx, lower=True)
        return -0.5 * x.size * jnp.log(2. * jnp.pi) - jnp.sum(jnp.log(jnp.diag(L))) \
               - 0.5 * dx @ dx

    ndims = 4
    prior_mu = 2 * jnp.ones(ndims)
    prior_cov = jnp.diag(jnp.ones(ndims)) ** 2

    data_mu = jnp.zeros(ndims)
    data_cov = jnp.diag(jnp.ones(ndims)) ** 2
    data_cov = jnp.where(data_cov == 0., 0.95, data_cov)

    true_logZ = log_normal(data_mu, prior_mu, prior_cov + data_cov)

    post_mu = prior_cov @ jnp.linalg.inv(prior_cov + data_cov) @ data_mu + data_cov @ jnp.linalg.inv(
        prior_cov + data_cov) @ prior_mu

    log_likelihood = lambda x, **kwargs: log_normal(x, data_mu, data_cov)

    with PriorChain() as prior_chain:
        MVNPrior('x', prior_mu, prior_cov)

    ns = NestedSampler(log_likelihood, prior_chain, dynamic=True)
    results = ns(key=random.PRNGKey(42),
                 dynamic_kwargs=dict(G=0.),
                 termination_evidence_uncert=5e-2,
                 termination_max_num_steps=30)
    print(results)
    print(post_mu)
    summary(results)
    plot_diagnostics(results)
    plot_cornerplot(results)
    assert jnp.isclose(results.log_Z_mean, true_logZ, atol= 1.75 * results.log_Z_uncert)


def test_gh21():
    num_samples = 10
    true_k = 1.
    true_theta = 0.5

    _gamma = np.random.gamma(true_k, true_theta, size=num_samples)
    samples = jnp.asarray(np.random.poisson(_gamma, size=num_samples))

    prior_k = 5.
    prior_theta = 0.3

    true_post_k = prior_k + jnp.sum(samples)
    true_post_theta = prior_theta / (num_samples * prior_theta + 1.)

    def log_likelihood(gamma, **kwargs):
        """
        Poisson likelihood.
        """
        return jnp.sum(samples * jnp.log(gamma) - gamma - gammaln(samples + 1))

    with PriorChain() as prior_chain:
        gamma = GammaPrior('gamma', prior_k, prior_theta)

    ns = NestedSampler(loglikelihood=log_likelihood, prior_chain=prior_chain)
    results = jit(ns)(random.PRNGKey(32564))

    samples = resample(random.PRNGKey(43083245), results.samples, results.log_dp_mean, S=int(results.ESS))

    sample_mean = jnp.mean(samples['gamma'], axis=0)

    true_mean = true_post_k * true_post_theta

    assert jnp.allclose(sample_mean, true_mean, atol=0.05)
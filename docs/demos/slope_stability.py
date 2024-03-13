# -*- coding: utf-8 -*-
# ---
# jupyter:
#   jupytext:
#     cell_metadata_filter: -all
#     custom_cell_magics: kql
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.11.2
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %%
# JSH: Comments on text.
# 1. The main point of this tutorial is to show how JAX AD can be used to
#    take away a lot of the by-hand differentiation. When I read this
#    intro, it just seems like something I could have done in e.g. MFront.
# 2. The equations should be put down where you define them in the code.

# AL: comments
# 1. How to explain the non-associativity for the problem where it is not required??
# 2. Maybe `FEMExternalOperator` is a good name for the framework?

# %% [markdown]
# # Slope stability problem
#
# This tutorial aims to demonstrate how modern automatic differentiation (AD)
# techniques may be used to define a complex constitutive model demanding a lot of
# by-hand differentiation. In particular, we implement the non-associative
# plasticity model of Mohr-Coulomb with apex-smoothing applied to a slope
# stability problem for soil. We use the JAX package to define constitutive
# relations including the differentiation of certain terms and
# `FEMExternalOperator` framework to incorporate this model into a weak
# formulation within UFL.
#
# The tutorial is based on the
# [limit analysis](https://fenics-optim.readthedocs.io/en/latest/demos/limit_analysis_3D_SDP.html)
# within semi-definite programming framework, where the plasticity model was
# replaced by the MFront/TFEL
# [implementation](https://thelfer.github.io/tfel/web/MohrCoulomb.html) of
# Mohr-Coulomb elastoplastic model with apex smoothing.
#
#
# ## Problem formulation
#
# We solve a slope stability problem of a soil domain $\Omega$ represented by a
# parallelepiped $[0; L] \times [0; W] \times [0; H]$ with homogeneous Dirichlet
# boundary conditions for the displacement field $\boldsymbol{u} = \boldsymbol{0}$
# on the right side $x = L$ and the bottom one $z = 0$. The loading consists of a
# gravitational body force $\boldsymbol{q}=[0, 0, -\gamma]^T$ with $\gamma$ being
# the soil self-weight. The solution of the problem is to find the collapse load
# $q_\text{lim}$, for which we know an analytical solution in the plane-strain
# case for the standard Mohr-Coulomb criterion [CITE] (TODO: rewrite later). We
# follow the same Mandel-Voigt notation as in the von Mises plasticity tutorial
# but in 3D.
#
# ```{note}
# Although the tutorial shows the implementation of the Mohr-Coulomb model, it
# is quite general to be adapted to a wide rage of plasticity models that may
# be defined through a yield surface and a plastic potential.
# ```
#
# ## Implementation
#
# ### Preamble

# %%
from mpi4py import MPI
from petsc4py import PETSc

import jax
import jax.lax
import jax.numpy as jnp
jax.config.update("jax_enable_x64", True)  # replace by JAX_ENABLE_X64=True

import matplotlib.pyplot as plt
import numpy as np
from solvers import LinearProblem
from utilities import build_cylinder_quarter, find_cell_by_point

import basix
import ufl
from dolfinx import common, fem, mesh, default_scalar_type, io
from dolfinx_external_operator import (
    FEMExternalOperator,
    evaluate_external_operators,
    evaluate_operands,
    replace_external_operators,
)


# %% [markdown]
# ### Model parameters
#
# Here we define geometrical and material parameters of the problem as well as
# some useful constants.

# %%
E = 6778  # [MPa] Young modulus
nu = 0.25  # [-] Poisson ratio

c = 3.45 # [MPa] cohesion
phi = 30 * np.pi / 180  # [rad] friction angle
psi = 30 * np.pi / 180  # [rad] dilatancy angle
theta_T = 26 * np.pi / 180  # [rad] transition angle as defined by Abbo and Sloan
a = 0.26 * c / np.tan(phi)  # [MPa] tension cuff-off parameter

# %%
L, W, H = (1.2, 2., 1.)
Nx, Ny, Nz = (10, 10, 10)
gamma = 1.
domain = mesh.create_box(MPI.COMM_WORLD, [np.array([0,0,0]), np.array([L, W, H])], [Nx, Ny, Nz])

# %%
k_u = 2
V = fem.functionspace(domain, ("Lagrange", k_u, (3,)))
# Boundary conditions
def on_right(x):
    return np.isclose(x[0], L)

def on_bottom(x):
    return np.isclose(x[2], 0.)

bottom_dofs = fem.locate_dofs_geometrical(V, on_bottom)
right_dofs = fem.locate_dofs_geometrical(V, on_right)
# bcs = [fem.dirichletbc(0.0, bottom_dofs, V), fem.dirichletbc(np.array(0.0, dtype=PETSc.ScalarType), right_dofs, V)] # bug???
bcs = [
    fem.dirichletbc(np.array([0.0, 0.0, 0.0], dtype=PETSc.ScalarType), bottom_dofs, V),
    fem.dirichletbc(np.array([0.0, 0.0, 0.0], dtype=PETSc.ScalarType), right_dofs, V)]

def epsilon(v):
    grad_v = ufl.grad(v)
    return ufl.as_vector([
        grad_v[0, 0], grad_v[1, 1], grad_v[2, 2],
        np.sqrt(2.0) * 0.5 * (grad_v[1, 2] + grad_v[2, 1]),
        np.sqrt(2.0) * 0.5 * (grad_v[0, 2] + grad_v[2, 0]),
        np.sqrt(2.0) * 0.5 * (grad_v[0, 1] + grad_v[1, 0]),
    ])

k_stress = 2 * (k_u - 1)

dx = ufl.Measure(
    "dx",
    domain=domain,
    metadata={"quadrature_degree": k_stress, "quadrature_scheme": "default"},
)

S_element = basix.ufl.quadrature_element(domain.topology.cell_name(), degree=k_stress, value_shape=(6,))
S = fem.functionspace(domain, S_element)


Du = fem.Function(V, name="Du")
u = fem.Function(V, name="Total_displacement")
du = fem.Function(V, name="du")
v = ufl.TrialFunction(V)
u_ = ufl.TestFunction(V)

sigma = FEMExternalOperator(epsilon(Du), function_space=S)
sigma_n = fem.Function(S, name="sigma_n")


# %% [markdown]
# ### Defining the constitutive model and the external operator
#
# The constitutive model of the soil is described by a non-associative plasticity
# law without hardening that is defined by the Mohr-Coulomb yield surface $f$ and
# the plastic potential $g$. Both quantities may be expressed through the
# following function $h$
#
# \begin{align*}
#     & h(\boldsymbol{\sigma}, \alpha) =
#     \frac{I_1(\boldsymbol{\sigma})}{3}\sin\alpha +
#     \sqrt{J_2(\boldsymbol{\sigma}) K^2(\alpha) + a^2(\alpha)\sin^2\alpha} -
#     c\cos\alpha, \\
#     & f(\boldsymbol{\sigma}) = h(\boldsymbol{\sigma}, \phi), \\
#     & g(\boldsymbol{\sigma}) = h(\boldsymbol{\sigma}, \psi),
# \end{align*}
# where $\phi$ and $\psi$ are friction and dilatancy angles, $c$ is a cohesion,
# $I_1(\boldsymbol{\sigma}) = \mathrm{tr} \boldsymbol{\sigma}$ is the first
# invariant of the stress tensor and $J_2(\boldsymbol{\sigma}) =
# \frac{1}{2}\boldsymbol{s}:\boldsymbol{s}$ is the second invariant of the
# deviatoric part of the stress tensor. The expression of the coefficient
# $K(\alpha)$ may be found in the MFront/TFEL
# [implementation](https://thelfer.github.io/tfel/web/MohrCoulomb.html).
#
# During the plastic loading the stress-strain state of the solid must satisfy
# the following system of nonlinear equations
#
# $$
#     \begin{cases}
#         \boldsymbol{r}_{g}(\boldsymbol{\sigma}_{n+1}, \Delta\lambda) =
#         \boldsymbol{\sigma}_{n+1} - \boldsymbol{\sigma}_n -
#         \boldsymbol{C}.(\Delta\boldsymbol{\varepsilon} - \Delta\lambda
#         \frac{\mathrm{d} g}{\mathrm{d}\boldsymbol{\sigma}}(\boldsymbol{\sigma_{n+1}})) =
#         \boldsymbol{0}, \\
#         r_f(\boldsymbol{\sigma}_{n+1}) = f(\boldsymbol{\sigma}_{n+1}) = 0,
#     \end{cases}
# $$ (eq_MC_1)
#
# By introducing the residual vector $\boldsymbol{r} = [\boldsymbol{r}_{g}^T,
# r_f]^T$ and its argument vector $\boldsymbol{x} = [\boldsymbol{\sigma}_{n+1}^T, \Delta\lambda]^T$ we solve the following nonlinear equation:
#
# $$
#     \boldsymbol{r}(\boldsymbol{x}_{n+1}) = \boldsymbol{0}
# $$
#
# To solve this equation we apply the Newton method and introduce the Jacobian of
# the residual vector $\boldsymbol{j} = \frac{\mathrm{d} \boldsymbol{r}}{\mathrm{d}
# \boldsymbol{x}}$. Thus we solve the following linear system at each quadrature
# point for the plastic phase
#
# $$
#     \begin{cases}
#         \boldsymbol{j}(\boldsymbol{x}_{n})\boldsymbol{y} = -
#         \boldsymbol{r}(\boldsymbol{x}_{n}), \\
#         \boldsymbol{x}_{n+1} = \boldsymbol{x}_n + \boldsymbol{y}.
#     \end{cases}
# $$
#
# During the elastic loading, we consider a trivial system of equations
#
# $$
#     \begin{cases}
#         \boldsymbol{\sigma}_{n+1} = \boldsymbol{\sigma}_n +
#         \boldsymbol{C}.\Delta\boldsymbol{\varepsilon}, \\ \Delta\lambda = 0.
#     \end{cases}
# $$ (eq_MC_2)
#
# The algorithm solving the systems {eq}`eq_MC_1`--{eq}`eq_MC_2` is called the
# return-mapping procedure and the solution defines the return-mapping
# correction of the stress tensor. By implementation of the external operator
# $\boldsymbol{\sigma}$ we mean the implementation of this procedure. 
#
# The automatic differentiation tools of the JAX library are applied to calculate
# the derivatives $\frac{\mathrm{d} g}{\mathrm{d}\boldsymbol{\sigma}}, \frac{\mathrm{d}
# \boldsymbol{r}}{\mathrm{d} \boldsymbol{x}}$ as well as the stress tensor
# derivative or the tangent stiffness matrix $\boldsymbol{C}_\text{tang} =
# \frac{\mathrm{d}\boldsymbol{\sigma}}{\mathrm{d}\boldsymbol{\varepsilon}}$.
#
# #### Defining yield surface and plastic potential
#
# First of all, we define supplementary functions that help us to express the
# yield surface $f$ and the plastic potential $g$. In the following definitions,
# we use built-in functions of the JAX package, in particular, the conditional
# primitive `jax.lax.cond`. It is necessary for the correct work of the AD tool
# and just-in-time compilation. For more details, please, visit the JAX
# [documentation](https://jax.readthedocs.io/en/latest/).

# %%
def J3(s):
    return s[2] * (s[0] * s[1] - s[3] * s[3] / 2.0)

def J2(s):
    return 0.5 * jnp.vdot(s, s)

def theta(s):
    J2_ = J2(s)
    arg = -(3.0 * np.sqrt(3.0) * J3(s)) / (2.0 * jnp.sqrt(J2_ * J2_ * J2_))
    arg = jnp.clip(arg, -1.0, 1.0)
    theta = 1.0 / 3.0 * jnp.arcsin(arg)
    return theta

def rho(s):
    return jnp.sqrt(2.0 * J2(s))

def sign(x):
    return jax.lax.cond(x < 0.0, lambda x: -1, lambda x: 1, x)

def coeff1(theta, angle):
    return np.cos(theta_T) - (1.0 / np.sqrt(3.0)) * np.sin(angle) * np.sin(theta_T)


def coeff2(theta, angle):
    return sign(theta) * np.sin(theta_T) + (1.0 / np.sqrt(3.0)) * np.sin(angle) * np.cos(theta_T)

coeff3 = 18.0 * np.cos(3.0 * theta_T) * np.cos(3.0 * theta_T) * np.cos(3.0 * theta_T)


def C(theta, angle):
    return (
        -np.cos(3.0 * theta_T) * coeff1(theta, angle)
        - 3.0 * sign(theta) * np.sin(3.0 * theta_T) * coeff2(theta, angle)
    ) / coeff3


def B(theta, angle):
    return (
        sign(theta) * np.sin(6.0 * theta_T) * coeff1(theta, angle)
        - 6.0 * np.cos(6.0 * theta_T) * coeff2(theta, angle)
    ) / coeff3


def A(theta, angle):
    return (
        -(1.0 / np.sqrt(3.0)) * np.sin(angle) * sign(theta) * np.sin(theta_T)
        - B(theta, angle) * sign(theta) * np.sin(3*theta_T)
        - C(theta, angle) * np.sin(3.0 * theta_T) * np.sin(3.0 * theta_T)
        + np.cos(theta_T)
    )

# def A(theta, angle):
#     return 1./3. * np.cos(theta_T) * (3 + np.tan(theta_T) * np.tan(3*theta_T) + 1./np.sqrt(3) * sign(theta) * (np.tan(3*theta_T) - 3*np.tan(theta_T)) * np.sin(angle))

# def B(theta, angle):
#     return 1./(3.*np.cos(3.*theta_T)) * (sign(theta) * np.sin(theta_T) + 1/np.sqrt(3) * np.sin(angle) * np.cos(theta_T))

def K(theta, angle):
    def K_false(theta):
        return jnp.cos(theta) - (1.0 / np.sqrt(3.0)) * np.sin(angle) * jnp.sin(theta)

    def K_true(theta):
        return (
            A(theta, angle)
            + B(theta, angle) * jnp.sin(3.0 * theta)
            + C(theta, angle) * jnp.sin(3.0 * theta) * jnp.sin(3.0 * theta)
        )
    # def K_true(theta):
    #     return (
    #         A(theta, angle) - B(theta, angle) * jnp.sin(3.0 * theta)
    #     )

    return jax.lax.cond(jnp.abs(theta) > theta_T, K_true, K_false, theta)


def a_g(angle):
    return a * np.tan(phi) / np.tan(angle)

dev = np.array(
        [
            [2.0 / 3.0, -1.0 / 3.0, -1.0 / 3.0, 0.0, 0.0, 0.0],
            [-1.0 / 3.0, 2.0 / 3.0, -1.0 / 3.0, 0.0, 0.0, 0.0],
            [-1.0 / 3.0, -1.0 / 3.0, 2.0 / 3.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
        ],
        dtype=PETSc.ScalarType,
    )
tr = np.array([1.0, 1.0, 1.0, 0.0, 0.0, 0.0], dtype=PETSc.ScalarType)

def surface(sigma_local, angle):
    # AL: Maybe it's more efficient to use untracable np.array?
    s = dev @ sigma_local
    I1 = tr @ sigma_local
    theta_ = theta(s)
    return (
        (I1 / 3.0 * np.sin(angle))
        + jnp.sqrt(J2(s) * K(theta_, angle) * K(theta_, angle) + a_g(angle) * a_g(angle) * np.sin(angle) * np.sin(angle))
        - c * np.cos(angle)
    )
    # return (I1 / 3.0 * np.sin(angle)) + jnp.sqrt(J2(s)) * K(theta_, angle) - c * np.cos(angle)

# %% [markdown]
# By picking up an appropriate angle we define the yield surface $F$ and the
# plastic potential $G$.

# %%
def f(sigma_local):
    return surface(sigma_local, phi)

def g(sigma_local):
    return surface(sigma_local, psi)

dgdsigma = jax.jacfwd(g)

# %% [markdown]
# #### Solving constitutive equations
#
# In this section, we define the constitutive model by solving the systems {eq}`eq_MC_1`--{eq}`eq_MC_2`. They must be solved at each Gauss point, so we apply the
# Newton method, implement the whole algorithm locally and then vectorize the
# final result using `jax.vmap`.
#
# In the following cell, we define locally the residual $\boldsymbol{r}$ and
# its jacobian `drdx`.

# %%
# NOTE: Actually, I put conditionals inside local functions, but we may
# implement two "branches" of the algo separetly and check the yielding
# condition in the main Newton loop. It may be more efficient, but idk. Anyway,
# as it is, it looks fancier.

lmbda = E * nu / ((1.0 + nu) * (1.0 - 2.0 * nu))
mu = E / (2.0 * (1.0 + nu))
C_elas = np.array([[lmbda+2*mu, lmbda, lmbda, 0, 0, 0],
                    [lmbda, lmbda+2*mu, lmbda, 0, 0, 0],
                    [lmbda, lmbda, lmbda+2*mu, 0, 0, 0],
                    [0, 0, 0, 2*mu, 0, 0],
                    [0, 0, 0, 0, 2*mu, 0],
                    [0, 0, 0, 0, 0, 2*mu],
                    ], dtype=PETSc.ScalarType)
S_elas = np.linalg.inv(C_elas)
ZERO_VECTOR = np.zeros(6, dtype=PETSc.ScalarType)

def deps_p(sigma_local, dlambda, deps_local, sigma_n_local):
    sigma_elas_local = sigma_n_local + C_elas @ deps_local
    yielding = f(sigma_elas_local)

    def deps_p_elastic(sigma_local, dlambda):
        return ZERO_VECTOR

    def deps_p_plastic(sigma_local, dlambda):
        return dlambda * dgdsigma(sigma_local)

    return jax.lax.cond(yielding <= 0.0, deps_p_elastic, deps_p_plastic, sigma_local, dlambda)


def r_g(sigma_local, dlambda, deps_local, sigma_n_local):
    deps_p_local = deps_p(sigma_local, dlambda, deps_local, sigma_n_local)
    return sigma_local - sigma_n_local - C_elas @ (deps_local - deps_p_local)


def r_f(sigma_local, dlambda, deps_local, sigma_n_local):
    sigma_elas_local = sigma_n_local + C_elas @ deps_local
    yielding = f(sigma_elas_local)

    def r_f_elastic(sigma_local, dlambda):
        return dlambda

    def r_f_plastic(sigma_local, dlambda):
        return f(sigma_local)

    # JSH: Why is this comparison with eps? eps is essentially 0.0 when doing
    # <=. AL: In the case of yielding = 1e-15 - 1e-16 (or we can choose the
    # tolerance), the plastic branch will be chosen, which is more expensive.
    return jax.lax.cond(yielding <= 0.0, r_f_elastic, r_f_plastic, sigma_local, dlambda)


def r(x_local, deps_local, sigma_n_local):
    sigma_local = x_local[:6]
    dlambda_local = x_local[-1]

    res_g = r_g(sigma_local, dlambda_local, deps_local, sigma_n_local)
    res_f = r_f(sigma_local, dlambda_local, deps_local, sigma_n_local)

    res = jnp.c_["0,1,-1", res_g, res_f]
    return res


drdx = jax.jacfwd(r) # Jacobian j

# %% [markdown]
# Then we define the function `return_mapping` that implements the
# return-mapping algorithm numerically via the Newton method.

# %%
Nitermax, tol = 200, 1e-8


# JSH: You need to explain somewhere here how the while_loop interacts with
# vmap.
ZERO_SCALAR = np.array([0.0])
def sigma_return_mapping(deps_local, sigma_n_local):
    """Performs the return-mapping procedure.

    It solves elastoplastic constitutive equations numerically by applying the
    Newton method in a single Gauss point. The Newton loop is implement via
    `jax.lax.while_loop`.
    """
    niter = 0

    dlambda = ZERO_SCALAR
    sigma_local = sigma_n_local
    x_local = jnp.concatenate([sigma_local, dlambda])

    res = r(x_local, deps_local, sigma_n_local)
    norm_res0 = jnp.linalg.norm(res)

    def cond_fun(state):
        norm_res, niter, _ = state
        return jnp.logical_and(norm_res / norm_res0 > tol, niter < Nitermax)

    def body_fun(state):
        norm_res, niter, history = state

        x_local, deps_local, sigma_n_local, res = history

        j = drdx(x_local, deps_local, sigma_n_local)
        j_inv_vp = jnp.linalg.solve(j, -res)
        x_local = x_local + j_inv_vp

        res = r(x_local, deps_local, sigma_n_local)
        norm_res = jnp.linalg.norm(res)
        history = x_local, deps_local, sigma_n_local, res

        niter += 1

        return (norm_res, niter, history)

    history = (x_local, deps_local, sigma_n_local, res)

    norm_res, niter_total, x_local = jax.lax.while_loop(cond_fun, body_fun, (norm_res0, niter, history))

    sigma_local = x_local[0][:6]
    sigma_elas_local = C_elas @ deps_local
    yielding = f(sigma_n_local + sigma_elas_local)

    dlambda = x_local[0][-1]

    return sigma_local, (sigma_local, niter_total, yielding, norm_res, dlambda)
    # return sigma_local, (sigma_local,)

def C_tang(deps_local, sigma_n_local, sigma_local, dlambda_local):
    x_local = jnp.c_["0,1,-1", sigma_local, dlambda_local]
    j = drdx(x_local, deps_local, sigma_n_local)
    H = jnp.linalg.inv(j)[:6,:6] @ C_elas
    return H

    # A = j[:4,:4]
    # n = j[4,:4] # dfdsigma
    # m = j[:4,4] # dgdsigma
    # H = jnp.linalg.inv(A) @ C_elas
    # term_tmp = n.T @ H @ m
    # term = jax.lax.cond(term_tmp == 0.0, lambda x : 1., lambda x: x, term_tmp)
    # return H - jnp.outer((H @ m), (H @ n)) / term, term

C_tang_v = jax.jit(jax.vmap(C_tang, in_axes=(0, 0, 0, 0)))

# %% [markdown]
# The `return_mapping` function returns a tuple with two elements. The first
# element is an array containing values of the external operator
# $\boldsymbol{\sigma}$ and the second one is another tuple containing
# additional data such as e.g. information on a convergence of the Newton
# method. Once we apply the JAX AD tool, the latter "converts" the first
# element of the `return_mapping` output into an array with values of the
# derivative
# $\frac{\mathrm{d}\boldsymbol{\sigma}}{\mathrm{d}\boldsymbol{\varepsilon}}$
# and leaves untouched the second one. That is why we return `sigma_local`
# twice in the `return_mapping`: ....
#
# COMMENT: Well, looks too wordy...
# JSH eg.
# `jax.jacfwd` returns a callable that returns the Jacobian as its first return
# argument. As we also need sigma_local, we also return sigma_local as
# auxilliary data.
#
#
# NOTE: If we implemented the function `dsigma_ddeps` manually, it would return
# `C_tang_local, (sigma_local, niter_total, yielding, norm_res)`

# %% [markdown]
# Once we defined the function `dsigma_ddeps`, which evaluates both the
# external operator and its derivative locally, we can just vectorize it and
# define the final implementation of the external operator derivative.

# %%
# dsigma_ddeps_vec = jax.jit(jax.vmap(sigma_return_mapping, in_axes=(0, 0)))

# def sigma_impl(deps):
#     deps_ = deps.reshape((-1, 6))
#     sigma_n_ = sigma_n.x.array.reshape((-1, 6))

#     (sigma_global, state) = dsigma_ddeps_vec(deps_, sigma_n_)
#     C_tang_global, niter, yielding, norm_res = state

#     unique_iters, counts = jnp.unique(niter, return_counts=True)

#     # NOTE: The following code prints some details about the second Newton
#     # solver, solving the constitutive equations. Do we need this or it's better
#     # to have the code as clean as possible?

#     print("\tInner Newton summary:")
#     print(f"\t\tUnique number of iterations: {unique_iters}")
#     print(f"\t\tCounts of unique number of iterations: {counts}")
#     print(f"\t\tMaximum F: {jnp.max(yielding)}")
#     print(f"\t\tMaximum residual: {jnp.max(norm_res)}")

#     return C_tang_global.reshape(-1), sigma_global.reshape(-1)

# %%
dsigma_ddeps = jax.jacfwd(sigma_return_mapping, has_aux=True)
dsigma_ddeps_vec = jax.jit(jax.vmap(dsigma_ddeps, in_axes=(0, 0)))


def C_tang_impl(deps):
    deps_ = deps.reshape((-1, 6))
    sigma_n_ = sigma_n.x.array.reshape((-1, 6))

    (C_tang_global, state) = dsigma_ddeps_vec(deps_, sigma_n_)
    sigma_global, niter, yielding, norm_res, dlambda = state

    # C_tang_tmp = C_tang_v(deps_, sigma_n_, sigma_global.reshape((-1, 4)), dlambda)

    # maxxx = -1.
    # i_max = 0
    # for i in range(len(C_tang_global.reshape(-1, 4, 4))):
    #     eps = np.abs(np.max(C_tang_tmp[i] - C_tang_global.reshape(-1, 4, 4)[i]))
    #     if eps > maxxx:
    #         maxxx = eps
    #         i_max = i
    # print(maxxx, '\n' , C_tang_global[i_max], '\n', C_tang_tmp[i_max])

    unique_iters, counts = jnp.unique(niter, return_counts=True)

    # NOTE: The following code prints some details about the second Newton
    # solver, solving the constitutive equations. Do we need this or it's better
    # to have the code as clean as possible?

    print("\tInner Newton summary:")
    print(f"\t\tUnique number of iterations: {unique_iters}")
    print(f"\t\tCounts of unique number of iterations: {counts}")
    print(f"\t\tMaximum F: {jnp.max(yielding)}")
    print(f"\t\tMaximum residual: {jnp.max(norm_res)}")

    return C_tang_global.reshape(-1), sigma_global.reshape(-1)

# %% [markdown]
# Similarly to the von Mises example, we do not implement explicitly the
# evaluation of the external operator. Instead, we obtain its values during the
# evaluation of its derivative and then update the values of the operator in the
# main Newton loop.

# %%
def sigma_external(derivatives):
    # if derivatives == (0,):
    #     return sigma_impl
    if derivatives == (1,):
        return C_tang_impl
    else:
        return NotImplementedError


sigma.external_function = sigma_external

# %% [markdown]
# ### Defining the forms

# %%
q = fem.Constant(domain, default_scalar_type((0, 0, -gamma)))

def F_ext(v):
    return ufl.dot(q, v) * dx


u_hat = ufl.TrialFunction(V)
F = ufl.inner(epsilon(u_), sigma) * dx - F_ext(u_)
J = ufl.derivative(F, Du, u_hat)
J_expanded = ufl.algorithms.expand_derivatives(J)

F_replaced, F_external_operators = replace_external_operators(F)
J_replaced, J_external_operators = replace_external_operators(J_expanded)

F_form = fem.form(F_replaced)
J_form = fem.form(J_replaced)

# %% [markdown]
# ### Variables initialization and compilation
# Before solving the problem it is required.

# %%
# Initialize variables to start the algorithm
# NOTE: Actually we need to evaluate operators before the Newton solver
# in order to assemble the matrix, where we expect elastic stiffness matrix
# Shell we discuss it? The same states for the von Mises.
Du.x.array[:] = 1.0  # any value allowing 
sigma_n.x.array[:] = 0.0

timer1 = common.Timer("1st JAX pass")
timer1.start()

evaluated_operands = evaluate_operands(F_external_operators)
((_, _),) = evaluate_external_operators(J_external_operators, evaluated_operands)

timer1.stop()

timer2 = common.Timer("2nd JAX pass")
timer2.start()

evaluated_operands = evaluate_operands(F_external_operators)
((_, _),) = evaluate_external_operators(J_external_operators, evaluated_operands)

timer2.stop()

timer3 = common.Timer("3rd JAX pass")
timer3.start()

evaluated_operands = evaluate_operands(F_external_operators)
((_, _),) = evaluate_external_operators(J_external_operators, evaluated_operands)

timer3.stop()

# %%
# TODO: Is there a more elegant way to extract the data?
# TODO: Maybe we analyze the compilation time in-place?
common.list_timings(MPI.COMM_WORLD, [common.TimingType.wall])

# %% [markdown]
# ### Solving the problem
#
# Summing up, we apply the Newton method to solve the main weak problem. On each
# iteration of the main Newton loop, we solve elastoplastic constitutive equations
# by using the second Newton method at each Gauss point. Thanks to the framework
# and the JAX library, the final interface is general enough to be reused for
# other plasticity models.

# %%
external_operator_problem = LinearProblem(J_replaced, -F_replaced, Du, bcs=bcs)

# %%
# Defining a cell containing (Ri, 0) point, where we calculate a value of u It
# is required to run this program via MPI in order to capture the process, to
# which this point is attached
x_point = np.array([[0, 0, H]])
cells, points_on_process = find_cell_by_point(domain, x_point)

# %%
xdmf_file = "output/limit_analysis.xdmf"
with io.XDMFFile(domain.comm, xdmf_file, "w") as xdmf:
    xdmf.write_mesh(domain)

# %%
# parameters of the manual Newton method
max_iterations, relative_tolerance = 200, 1e-8
# load_steps_2 = np.linspace(36.7, 36.83, 2)[1:]
# load_steps_3 = np.linspace(36.83, 36.84, 5)[1:]
load_steps_1 = np.linspace(3, 14, 15)
load_steps_2 = np.linspace(14, 20, 15)[1:]
load_steps_3 = np.linspace(20, 22, 10)[1:]
load_steps_4 = np.linspace(22, 22.5, 10)[1:]
load_steps = np.concatenate([load_steps_1, load_steps_2, load_steps_3, load_steps_4])
# load_steps_1 = np.linspace(1, 8, 30)
# load_steps = load_steps_1
num_increments = len(load_steps)
results = np.zeros((num_increments + 1, 2))

for i, load in enumerate(load_steps):
    f.value = load * np.array([0, 0, -gamma])
    external_operator_problem.assemble_vector()

    residual_0 = external_operator_problem.b.norm()
    residual = residual_0
    Du.x.array[:] = 0

    if MPI.COMM_WORLD.rank == 0:
        print(f"Load increment #{i}, load: {load}, initial residual: {residual_0}")

    for iteration in range(0, max_iterations):
        if residual / residual_0 < relative_tolerance:
            break

        if MPI.COMM_WORLD.rank == 0:
            print(f"\tOuter Newton iteration #{iteration}")
        external_operator_problem.assemble_matrix()
        external_operator_problem.solve(du)

        Du.vector.axpy(1.0, du.vector)
        Du.x.scatter_forward()

        evaluated_operands = evaluate_operands(F_external_operators)
        ((_, sigma_new),) = evaluate_external_operators(J_external_operators, evaluated_operands)
        # ((C_tang_new, sigma_new),) = evaluate_external_operators(F_external_operators, evaluated_operands)
        sigma.ref_coefficient.x.array[:] = sigma_new
        # J_external_operators[0].ref_coefficient.x.array[:] = C_tang_new

        external_operator_problem.assemble_vector()
        residual = external_operator_problem.b.norm()

        if MPI.COMM_WORLD.rank == 0:
            print(f"\tResidual: {residual}\n")

    u.vector.axpy(1.0, Du.vector)
    u.x.scatter_forward()

    sigma_n.x.array[:] = sigma.ref_coefficient.x.array

    # u2.interpolate(u)

    # with io.XDMFFile(domain.comm, xdmf_file, "a") as xdmf:
    #     xdmf.write_function(u2, i)

    if len(points_on_process) > 0:
        results[i + 1, :] = (u.eval(points_on_process, cells)[0], load)

print(f"Slope stability factor: {f.value[-1]*H/c}")

# %%
# 20 - critical load # -5.884057971014492
#Slope stability factor: -6.521739130434782


# %%
f.value

# %% [markdown]
# ## Post-processing

# %%
22.5*H/c

# %%
if len(points_on_process) > 0:
    plt.plot(results[:, 0], results[:, 1], "o-", label="via ExternalOperator")
    plt.xlabel("Displacement of inner boundary")
    plt.ylabel(r"Applied pressure $q/q_{lim}$")
    plt.savefig(f"displacement_rank{MPI.COMM_WORLD.rank:d}.png")
    # plt.legend()
    plt.show()


# %% [markdown]
# ## Verification

# %%
def rho(sigma_local):
    s = dev @ sigma_local
    return jnp.sqrt(2.0 * J2(s))

def angle(sigma_local):
    s = dev @ sigma_local
    arg = -(3.0 * jnp.sqrt(3.0) * J3(s)) / (2.0 * jnp.sqrt(J2(s) * J2(s) * J2(s)))
    arg = jnp.clip(arg, -1.0, 1.0)
    angle = 1.0 / 3.0 * jnp.arcsin(arg)
    return angle

def sigma_tracing(sigma_local, sigma_n_local):
    deps_elas = S_elas @ sigma_local
    sigma_corrected, state = sigma_return_mapping(deps_elas, sigma_n_local)
    yielding = state[2]
    return sigma_corrected, yielding

angle_v = jax.jit(jax.vmap(angle, in_axes=(0)))
rho_v = jax.jit(jax.vmap(rho, in_axes=(0)))
sigma_tracing_vec = jax.jit(jax.vmap(sigma_tracing, in_axes=(0, 0)))

# %%
N_angles = 200
N_loads = 10
angle_values = np.linspace(0, 2*np.pi, N_angles)
# angle_values = np.concatenate([np.linspace(-np.pi/6, np.pi/6, 100), np.linspace(5*np.pi/6, 7*np.pi/6, 100)])
R_values = np.linspace(0.4, 5, N_loads)
p = 2.

dsigma_paths = np.zeros((N_loads, N_angles, 4))

for i, R in enumerate(R_values):
    dsigma_paths[i,:,0] = np.sqrt(2./3.) * R * np.cos(angle_values)
    dsigma_paths[i,:,1] = np.sqrt(2./3.) * R * np.sin(angle_values - np.pi/6.)
    dsigma_paths[i,:,2] = np.sqrt(2./3.) * R * np.sin(-angle_values - np.pi/6.)

# %%
angle_results = np.empty((N_loads, N_angles))
rho_results = np.empty((N_loads, N_angles))
sigma_results = np.empty((N_loads, N_angles, 4))
sigma_n_local = np.full((N_angles, 4), p)
derviatoric_axis = np.array([1,1,1,0])

for i, R in enumerate(R_values):
    print(f"Loading#{i} {R}")
    dsigma, yielding = sigma_tracing_vec(dsigma_paths[0], sigma_n_local)
    p_tmp = dsigma @ tr / 3.
    dp = p_tmp - p
    dsigma -= np.outer(dp, derviatoric_axis)
    p_tmp = dsigma @ tr / 3.
    sigma_results[i,:] = dsigma
    print(f"{jnp.max(yielding)} {np.max(p_tmp)}\n")
    angle_results[i,:] = angle_v(dsigma)
    rho_results[i,:] = rho_v(dsigma)
    sigma_n_local[:] = dsigma


# Show the plot
plt.show()

# %%
fig, ax = plt.subplots(nrows=1, ncols=2, subplot_kw={'projection': 'polar'}, figsize=(10, 15))
# fig.suptitle(r'$\pi$-plane or deviatoric plane or octahedral plane, $\sigma (\rho=\sqrt{2J_2}, \theta$)')
for i in range(N_loads):
    ax[0].plot(angle_results[i], rho_results[i], '.', label='Load#'+str(i))
    ax[0].plot(np.repeat(np.pi/6, 10), np.linspace(0, np.max(rho_results), 10), color='black')
    ax[0].plot(np.repeat(-np.pi/6, 10), np.linspace(0, np.max(rho_results), 10), color='black')
    ax[1].plot(angle_values, rho_v(dsigma_paths[i]), '.', label='Load#'+str(i))
    ax[0].set_title(r'Octahedral profile of the yield criterion, $(\rho=\sqrt{2J_2}, \theta)$')
    ax[1].set_title(r'Paths of the loading $\sigma$, $(\rho=\sqrt{2J_2}, \theta)$')
plt.legend()
fig.tight_layout()

# %%

# %%
fig = plt.figure(figsize=(15,10))
# fig.suptitle(r'$\pi$-plane or deviatoric plane or octahedral plane, $\sigma (\rho=\sqrt{2J_2}, \theta$)')
ax1 = fig.add_subplot(221, polar=True)
ax2 = fig.add_subplot(222, polar=True)
ax3 = fig.add_subplot(223, projection='3d')
ax4 = fig.add_subplot(224, projection='3d')
for j in range(12):
    for i in range(N_loads):
        ax1.plot(j*np.pi/3 - j%2 * angle_results[i] + (1 - j%2) * angle_results[i], rho_results[i], '.', label='Load#'+str(i))
for i in range(N_loads):
    ax2.plot(angle_values, rho_v(dsigma_paths[i]), '.', label='Load#'+str(i))
    ax3.plot(sigma_results[i,:,0], sigma_results[i,:,1], sigma_results[i,:,2], '.')
    ax4.plot(sigma_results[i,:,0], sigma_results[i,:,1], sigma_results[i,:,2], '.')

ax1.plot(np.repeat(np.pi/6, 10), np.linspace(0, np.max(rho_results), 10), color='black')
ax1.plot(np.repeat(-np.pi/6, 10), np.linspace(0, np.max(rho_results), 10), color='black')
z_min = np.min(sigma_results[:,:,2])
z_max = np.max(sigma_results[:,:,2])
ax4.plot(np.array([p,p]), np.array([p,p]), np.array([z_min, z_max]), linestyle='-', color='black')

ax1.set_title(r'Octahedral profile of the yield criterion, $(\rho=\sqrt{2J_2}, \theta)$')
ax2.set_title(r'Paths of the loading $\sigma$, $(\rho=\sqrt{2J_2}, \theta)$')
ax3.view_init(azim=45)

for ax in [ax3, ax4]:
    ax.set_xlabel(r'$\sigma_{I}$')
    ax.set_ylabel(r'$\sigma_{II}$')
    ax.set_zlabel(r'$\sigma_{III}$')
    ax.set_title(r'In $(\sigma_{I}, \sigma_{II}, \sigma_{III})$ space')
plt.legend()
fig.tight_layout()

# %%
# TODO: Is there a more elegant way to extract the data?
# common.list_timings(MPI.COMM_WORLD, [common.TimingType.wall])

# %%
# # NOTE: There is the warning `[WARNING] yaksa: N leaked handle pool objects`
# for # the call `.assemble_vector()` and `.vector`. # NOTE: The following
# lines eleminate the leakes (except the mesh ones). # NOTE: To test this for
# the newest version of the DOLFINx.
external_operator_problem.__del__()
Du.vector.destroy()
du.vector.destroy()
u.vector.destroy()



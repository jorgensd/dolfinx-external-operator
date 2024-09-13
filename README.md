![License: LGPLv3](https://img.shields.io/badge/License-LGPL%20v3.0-lightgrey.svg)

# dolfinx-external-operator

`dolfinx-external-operator` is a implementation of the [external
operator](https://doi.org/10.48550/arXiv.2111.00945) concept in
[DOLFINx](https://github.com/FEniCS/dolfinx).

It allows for the expression of operators/functions in FEniCS that cannot be
easily written in the [Unified Form Language](https://github.com/fenics/ufl).

Potential application areas include complex constitutive models in solid and
fluid mechanics, neural network constitutive models, multiscale modelling and
inverse problems. 

Implementations of external operators can be written in any library that
supports the [array interface
protocol](https://numpy.org/doc/stable/reference/arrays.interface.html) e.g. 
[numpy](https://numpy.org/), [JAX](https://github.com/google/jax) and
[Numba](http://numba.pydata.org).

When using a library that supports program level automatic differentiation
(AD), such as JAX, it is possible to automatically derive derivatives for use
in local first and second-order solvers. Just-in-time compilation, batching and
accelerators (GPUs, TPUs) are also supported.

## Documentation

The [documentation](https://a-latyshev.github.io/dolfinx-external-operator/)
contains various examples focusing on complex constitutive behaviour in solid
mechanics, including:

* von Mises plasticity using [Numba](https://numba.pydata.org/),
* Mohr-Coulomb plasticity using [JAX](https://jax.readthedocs.io/en/latest).

## Installation

`dolfinx-external-operator` is a pure Python module that depends on DOLFINx
Python and UFL.

The latest release version can be installed with:

```Shell
pip install git+https://github.com/a-latyshev/dolfinx-external-operator.git@v0.8.0
```

The latest development version can be installed with:

```Shell
git clone https://github.com/a-latyshev/dolfinx-external-operator.git
cd dolfinx-external-operator
pip install -e .
```

## Citations 

If you use `dolfinx-external-operator` in your research we ask that you cite
the following references:

```
@inproceedings{latyshev_2024_external_paper,
	author = {Latyshev, Andrey and Bleyer, Jérémy and Hale, Jack and Maurini, Corrado},
	title = {A framework for expressing general constitutive models in FEniCSx},
    booktitle = {16ème Colloque National en Calcul de Structures},
	year = {2024},
    month = {May},
    publisher = {CNRS, CSMA, ENS Paris-Saclay, CentraleSupélec},
	address = {Giens, France},
    url = {https://hal.science/hal-04610881}
}
```

```
@software{latyshev_2024_external_code,
  title = {a-latyshev/dolfinx-external-operator},
  author = {Latyshev, Andrey and Hale, Jack},
  date = {2024},
  doi = {10.5281/zenodo.10907417}
  organization = {Zenodo}
}
```

## Developer notes

### Building Documentation

```Shell
pip install .[docs]
cd docs/
jupyter-book build .
```

and follow the instructions printed.

To continuously build and view the documentation in a web browser

```Shell
pip install sphinx-autobuild
cd build/
jupyter-book config sphinx .
sphinx-autobuild . _build/html -b html
```

To check and fix formatting

```Shell
pip install `.[lint]`
ruff check .
ruff format .
```

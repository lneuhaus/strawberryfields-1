# Copyright 2019 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Quantum operation parameters
============================

**Module name:** :mod:`strawberryfields.parameters`

.. currentmodule:: strawberryfields.parameters

The :class:`Parameter` class encapsulates a parameter passed to the
quantum operations represented by :class:`.Operation`.
There are three basic types of Parameters:

1. Numerical parameters (bound and fixed): An immediate, immutable numerical object (float, complex,
   int, array).
   NOTE: currently implemented as-is, not using the Parameter class. This can be changed if necessary,
   in which case most of the functions in this module would become Parameter class methods.
2. Measured parameters (bound but not fixed): Certain quantum circuits/protocols require that
   Operations can be conditioned on measurement results obtained during the execution of the
   circuit. In this case the parameter value is not known/fixed until the measurement is made
   (or simulated).
   Measured parameters are constructed from the :class:`RegRef` instance storing the measurement
   result using the :meth:`RegRef.par` method.
3. Free parameters (not bound nor fixed): A *parametrized circuit template* is a circuit that
   depends on a number of unbound (free) parameters. These parameters need to be bound to fixed
   numerical values before the circuit can be executed on a hardware quantum device or a numeric
   simulator.
   Simulators with symbolic capability can accept a parametrized circuit as input (and should
   return symbolic expressions representing the measurement results, with the same free parameters,
   as output).
   Free parameters belong to a single :class:`Program` instance, and are constructed using the
   :meth:`Program.args` method.

The Operations can accept parameters that are functions or arithmetic combinations of any number of
these basic types of Parameters.


The normal lifecycle of an Operation object and its associated Parameter instances is as follows:

* An Operation instance is constructed, and given some input arguments.
  :meth:`.Operation.__init__` converts the inputs into Parameter instances.
  The RegRef dependencies of Measured parameters are added to :attr:`Operation._measurement_deps`.

* The Operation instance is applied using its :meth:`~ops.Operation.__or__`
  method inside a :class:`.Program` context.
  This creates a :class:`.Command` instance that wraps
  the Operation and the RegRefs it acts on, which is appended to :attr:`.Program.circuit`.

* Before the Program is run, it is compiled and optimized for a specific backend. This involves
  checking that the Program only contains valid Operations, decomposing non-elementary Operations
  using :meth:`~ops.Operation.decompose`, and finally merging and commuting Commands inside
  the graph representing the quantum circuit.
  The circuit graph is built using the knowledge of which subsystems the Commands act and depend on.

* Merging two :class:`.Gate` instances of the same subclass involves
  adding their first parameters after equality-comparing the others. This is easily done if
  all the parameters have an immediate numerical value.
  Measured and free parameters are more complicated, but are handled by Sympy.
  TODO test/merge Operations that depend on RegRefTransforms or TensorFlow objects.

* The compiled Program is run by a :class:`.BaseEngine` instance, which calls the
  :meth:`~ops.Operation.apply` method of each Operation in turn.

* :meth:`Operation.apply` evaluates the numeric value of any RegRefTransform-based Parameters
  using :meth:`Parameter.evaluate` (other types of Parameters are simply passed through).
  The parameter values and the subsystem indices are passed to :meth:`Operation._apply`.

* :meth:`~ops.Operation._apply` "unwraps" the Parameter instances. There are three different cases:

  1. We still need to do some arithmetic, unwrap after it is done using `p.x`.
  2. No arithmetic required, use :func:`~parameters._unwrap`.
  3. No parameters are used, do nothing.

  Finally, :meth:`_apply` calls the appropriate backend API method using the unwrapped parameters.
  It is up to the backend to either accept NumPy arrays and Tensorflow objects as parameters, or not.

What we cannot do at the moment:

* Use anything except integers and RegRefs (or Sequences thereof) as the subsystem parameter
  for the :meth:`~ops.Operation.__or__` method.
  Technically we could allow any Parameters or valid Parameter initializers that evaluate into an integer.


Parameter methods
-----------------

.. currentmodule:: strawberryfields.parameters.Parameter

.. autosummary::
   evaluate


Code details
~~~~~~~~~~~~

"""
from collections.abc import Sequence
import numbers

import numpy as np
import sympy
import sympy.functions as parfuncs  # functions for manipulating the Parameters


class ParameterError(RuntimeError):
    """Exception raised when the Parameter classes encounter an illegal operation.

    E.g., trying to use a measurement result before it is available.
    """


def _evaluate(params):
    """Evaluate a parameter sequence.

    Any parameters descending from sympy.Basic are evaluated, others are returned as is.

    Args:
      params (Sequence[Any]): parameters to evaluate

    Returns:
      list[Any]: evaluated parameters
    """
    scalar = False
    if not isinstance(params, Sequence):
        scalar = True
        params = [params]
    def xxx(p):
        if not is_symbolic_par(p):
            return p
        p = p.evalf()
        # TODO bind free params: p.evalf(subs=free_param_dict)
        # TODO the float() conversion prevents symbolic params from being passed through, maybe the backend should do float() conversion?
        if not p.is_real:
            return complex(p)
        elif p.is_integer:
            return int(p)
        return float(p)

    ret = list(map(xxx, params))
    if scalar:
        return ret[0]
    return ret


def is_symbolic_par(p):
    """Returns True iff p is a symbolic parameter instance."""
    return isinstance(p, sympy.Basic)


def get_measurement_deps(p):
    """RegRef dependencies of an Operation parameter.

    Returns the RegRefs that the parameter p depends on through the :class:`MeasuredParameter`
    atoms it contains

    Args:
        p (Any): Operation parameter

    Returns:
        set[RegRef]:
    """
    ret = set()
    if not is_symbolic_par(p):
        return ret

    # p is a Sympy expression, possibly containing measured parameters
    for k in p.atoms(MeasuredParameter):
        ret.add(k.regref)
    return ret


def get_par_str(p):
    """String representation of the Operation parameter.

    Args:
        p (Any): Operation parameter

    Returns:
        str: string representation
    """
    if is_symbolic_par(p):
        return str(p)
    return '{:.4g}'.format(p)  # numeric parameters




#class MeasuredParameter(sympy.AtomicExpr):  # something is messed up in Sympy caching, maybe, frontend tests fail depending on their execution order
class MeasuredParameter(sympy.Symbol):
    """Single measurement result used as an Operation parameter.

    A MeasuredParameter instance, given as a parameter to a
    :class:`~strawberryfields.ops.Operation` constructor, represents
    a dependence of the Operation on classical information obtained by
    measuring a subsystem of the register.

    Used for deferred measurements, i.e., using a measurement's value
    symbolically in defining a gate before the numeric value of that
    measurement is available.

    Replaces RegRefTransforms.
    Arbitrary functions of atomic MeasuredParameters

    Args:
        regref (RegRef): register reference responsible for storing the measurement result
    """

    def __new__(cls, *args):
        # do not pass args to sympy.Basic.__new__ so they do not end up in self._args
        obj = super().__new__(cls, 'q'+str(args[0].ind))
        #obj = super().__new__(cls)
        return obj

    def __init__(self, regref):
        if not regref.active:
            # TODO: Maybe we want to delete a mode right after measurement to save comp effort?
            # The measurement result would still be preserved in the RegRef...
            raise ValueError('Trying to use an inactive RegRef.')
        #: RegRef: the parameter value depends on this RegRef, it can only be evaluated after the corresponding subsystem has been measured
        self.regref = regref

    def _sympystr(self, printer):
        """"The Sympy printing system uses this method instead of __str__."""
        return str(self.regref) + '.par'

    def _eval_evalf(self, prec):
        """Returns the numeric result of the measurement if it is available.

        Returns:
            sympy.Number: measurement result
        """
        res = self.regref.val
        if res is None:
            raise ParameterError("Trying to use a nonexistent measurement result (e.g., before it has been measured).")
        return sympy.Number(res)



class FreeParameter(sympy.Symbol):
    """Symbolic, unbound Operation parameter.

    Args:
        name (str): name of the free parameter
    """
    def __init__(self, name):
        #: str: name of the free parameter
        self.name = name
        #: Program, None: the Program owning this free parameter instance
        self.owner = None

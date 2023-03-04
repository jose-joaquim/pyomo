#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright (c) 2008-2022
#  National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

from pyomo.common.collections import ComponentMap
from pyomo.common.dependencies import numpy as np
from pyomo.common.dependencies.scipy import spatial
from pyomo.contrib.piecewise.piecewise_linear_expression import (
    PiecewiseLinearExpression)
from pyomo.core import Any, NonNegativeIntegers, value, Var
from pyomo.core.base.block import _BlockData, Block
from pyomo.core.base.component import ModelComponentFactory
from pyomo.core.base.expression import Expression
from pyomo.core.base.global_set import UnindexedComponent_index
from pyomo.core.base.indexed_component import UnindexedComponent_set
from pyomo.core.base.initializer import Initializer
import pyomo.core.expr.current as EXPR

# This is the default absolute tolerance in np.isclose... Not sure if it's
# enough, but we need to make sure that 'barely negative' values are assumed to
# be zero.
ZERO_TOLERANCE = 1e-8

class PiecewiseLinearFunctionData(_BlockData):
    _Block_reserved_words = Any

    def __init__(self, component=None):
        _BlockData.__init__(self, component)

        with self._declare_reserved_components():
            self._expressions = Expression(NonNegativeIntegers)
            self._transformed_exprs = ComponentMap()
            self._simplices = None
            # These will always be tuples, even when we only have one dimension.
            self._points = []
            self._linear_functions = []

    def __call__(self, *args):
        if all(type(arg) in EXPR.native_types or not
               arg.is_potentially_variable() for arg in args):
            # We need to actually evaluate
            return self._evaluate(*args)
        else:
            expr = PiecewiseLinearExpression(args, self)
            idx = id(expr)
            self._expressions[idx] = expr
            return self._expressions[idx]

    def _evaluate(self, *args):
        # ESJ: This is a very inefficient implementation in high dimensions, but
        # for now we will just do a linear scan of the simplices.
        if self._simplices is None:
            raise RuntimeError("Cannot evaluate PiecewiseLinearFunction--it "
                               "appears it is not fully defined. (No simplices "
                               "are stored.)")

        pt = [value(arg) for arg in args]
        for simplex, func in zip(self._simplices, self._linear_functions):
            if self._pt_in_simplex(pt, simplex):
                return func(*args)

        raise ValueError("Unsuccessful evaluation of PiecewiseLinearFunction "
                         "'%s' at point (%s). Is the point in the function's "
                         "domain?" %
                         (self.name, ', '.join(str(arg) for arg in args)))

    def _pt_in_simplex(self, pt, simplex):
        dim = len(pt)
        if dim == 1:
            return self._points[simplex[0]][0] <= pt[0] and \
                self._points[simplex[1]][0] >= pt[0]
        # Otherwise, we check if pt is a convex combination of the simplex's
        # extreme points
        A = np.ones((dim + 1, dim + 1))
        b = np.array([x for x in pt] + [1])
        for j, extreme_point in enumerate(simplex):
            for i, coord in enumerate(self._points[extreme_point]):
                A[i, j] = coord
        if np.linalg.det(A) == 0:
            # A is singular, so the system has no solutions
            return False
        else:
            lambdas = np.linalg.solve(A, b)
        for l in lambdas:
            if l < -ZERO_TOLERANCE:
                return False
        return True

    def _get_simplices_from_arg(self, simplices):
        self._simplices = []
        known_points = set()
        point_to_index = {}
        for simplex in simplices:
            extreme_pts = []
            for pt in simplex:
                if pt not in known_points:
                    known_points.add(pt)
                    if hasattr(pt, '__len__'):
                        self._points.append(pt)
                    else:
                        self._points.append((pt,))
                    point_to_index[pt] = len(self._points) - 1
                extreme_pts.append(point_to_index[pt])
            self._simplices.append(tuple(extreme_pts))

    def map_transformation_var(self, pw_expr, v):
        self._transformed_exprs[self._expressions[id(pw_expr)]] = v

    def get_transformation_var(self, pw_expr):
        if pw_expr in self._transformed_exprs:
            return self._transformed_exprs[pw_expr]
        else:
            return None


@ModelComponentFactory.register("Multidimensional piecewise linear function")
class PiecewiseLinearFunction(Block):
    """A piecewise linear function, which may be defined over an index.

    Can be specified in one of several ways:
        1) List of points and a nonlinear function to approximate. In
           this case, the points will be used to derive a triangulation
           of the part of the domain of interest, and a linear function
           approximating the given function will be calculated for each
           of the simplices in the triangulation. In this case, scipy is
           required (for multivariate functions).
        2) List of simplices and a nonlinear function to approximate. In
           this case, a linear function approximating the given function
           will be calculated for each simplex. For multivariate functions,
           numpy is required.
        3) List of simplices and list of functions that return linear function
           expressions. These are the desired piecewise functions
           corresponding to each simplex.

    Args:
        function: Nonlinear function to approximate, given as a Pyomo
            expression
        function_rule: Function that returns a nonlinear function to
            approximate for each index in an IndexedPiecewiseLinearFunction
        points: List of points in the same dimension as the domain of the
            function being approximated. Note that if the pieces of the
            function are specified this way, we require scipy.
        simplices: A list of lists of points, where each list specifies the
            extreme points of a a simplex over which the nonlinear function
            will be approximated as a linear function.
        linear_functions: A list of functions, each of which returns an
            expression for a linear function of the arguments.
    """
    _ComponentDataClass = PiecewiseLinearFunctionData

    def __new__(cls, *args, **kwds):
        if cls != PiecewiseLinearFunction:
            return super(PiecewiseLinearFunction, cls).__new__(cls)
        if not args or (args[0] is UnindexedComponent_set and len(args)==1):
            return PiecewiseLinearFunction.__new__(
                ScalarPiecewiseLinearFunction)
        else:
            return IndexedPiecewiseLinearFunction.__new__(
                IndexedPiecewiseLinearFunction)

    def __init__(self, *args, **kwargs):
        self._handlers = {
            # (f, pts, simplices, linear_funcs) : handler
            (True, True, False,
             False): self._construct_from_function_and_points,
            (True, False, True,
             False): self._construct_from_function_and_simplices,
            (False, False, True,
             True): self._construct_from_linear_functions_and_simplices
        }
        # [ESJ 1/24/23]: TODO: Eventually we should also support constructing
        # this from table data--a mapping of points to function values.

        _func_arg = kwargs.pop('function', None)
        _func_rule_arg = kwargs.pop('function_rule', None)
        _points_arg = kwargs.pop('points', None)
        _simplices_arg = kwargs.pop('simplices', None)
        _linear_functions = kwargs.pop('linear_functions', None)

        kwargs.setdefault('ctype', PiecewiseLinearFunction)
        Block.__init__(self, *args, **kwargs)

        # This cannot be a rule.
        self._func = _func_arg
        self._func_rule = Initializer(_func_rule_arg)
        self._points_rule = Initializer(_points_arg,
                                        treat_sequences_as_mappings=False)
        self._simplices_rule = Initializer(_simplices_arg,
                                           treat_sequences_as_mappings=False)
        self._linear_funcs_rule = Initializer(_linear_functions,
                                              treat_sequences_as_mappings=False)

    def _construct_from_function_and_points(self, obj, parent,
                                            nonlinear_function):
        parent = obj.parent_block()
        idx = obj._index

        points = self._points_rule(parent, idx)
        if len(points) < 1:
            raise ValueError("Cannot construct PiecewiseLinearFunction from "
                             "points list of length 0.")

        if hasattr(points[0], '__len__'):
            dimension = len(points[0])
        else:
            dimension = 1

        if dimension == 1:
            # This is univariate and we'll handle it separately in order to
            # avoid a dependence on numpy.
            points.sort()
            obj._simplices = []
            for i in range(len(points) - 1):
                obj._simplices.append((i, i + 1))
                obj._points.append((points[i],))
            # Add the last one
            obj._points.append((points[-1],))
            return self._construct_from_univariate_function_and_segments(
                obj, nonlinear_function)

        try:
            triangulation = spatial.Delaunay(points)
        except (spatial.QhullError, ValueError) as error:
            logger.error("Unable to triangulate the set of input points.")
            raise

        obj._points = [pt for pt in points]
        obj._simplices = [simplex for simplex in map(tuple,
                                                     triangulation.simplices)]

        return self._construct_from_function_and_simplices(obj, parent,
                                                           nonlinear_function)

    def _construct_from_univariate_function_and_segments(self, obj, func):
        # [ESJ 1/21/23]: See this blog post about why tje below is necessary:
        # https://eev.ee/blog/2011/04/24/gotcha-python-scoping-closures/
        # Basically, Python scoping is such a disaster that if we directly
        # declare the lambda function in the loop, their defintions will
        # rely on the value of idx1 and idx2... So all the functions will be
        # the last iteration function. By using a factory, we put 'slope' and
        # 'intercept' in a separate scope and get around this.
        def linear_func_factory(slope, intercept):
            return lambda x : slope*x + intercept

        for idx1, idx2 in obj._simplices:
            x1 = obj._points[idx1][0]
            x2 = obj._points[idx2][0]
            y = {x : func(x) for x in [x1, x2]}
            slope = (y[x2] - y[x1])/(x2 - x1)
            intercept = y[x1] - slope*x1
            obj._linear_functions.append(linear_func_factory(slope, intercept))

        return obj

    def _construct_from_function_and_simplices(self, obj, parent,
                                               nonlinear_function):
        if obj._simplices is None:
            obj._get_simplices_from_arg(self._simplices_rule(parent,
                                                             obj._index))
        simplices = obj._simplices

        if len(simplices) < 1:
            raise ValueError("Cannot construct PiecewiseLinearFunction "
                             "with empty list of simplices")

        dimension = len(simplices[0]) - 1
        if dimension == 1:
            # Back to high school with us--this is univariate and we'll handle
            # it separately in order to avoid a kind of silly dependence on
            # numpy.
            return self._construct_from_univariate_function_and_segments(
                obj, nonlinear_function)

        def linear_function_factory(normal):
            def f(*args):
                return sum(normal[i]*arg for i, arg in enumerate(args)) + \
                    normal[-1]
            return f

        # evaluate the function at each of the points and form the homogeneous
        # system of equations
        A = np.ones((dimension + 2, dimension + 2))
        b = np.zeros(dimension + 2)
        b[-1] = 1

        for num_piece, simplex in enumerate(simplices):
            for i, pt_idx in enumerate(simplex):
                pt = obj._points[pt_idx]
                for j, val in enumerate(pt):
                    A[i, j] = val
                A[i, j + 1] = nonlinear_function(*pt)
            A[i + 1, :] = 0
            A[i + 1, dimension] = -1
            # This system has a solution unless there's a bug--we know there is
            # a hyperplane that passes through dimension + 1 points (and the
            # last equation scales it so that the coefficient for the output
            # of the nonlinear function dimension is -1, so we can just read
            # off the linear equation in the x space).
            normal = np.linalg.solve(A, b)
            obj._linear_functions.append(linear_function_factory(normal))

        return obj

    def _construct_from_linear_functions_and_simplices(self, obj, parent,
                                                       nonlinear_function):
        # We know that we have simplices because else this handler wouldn't
        # have been called.
        obj._get_simplices_from_arg(self._simplices_rule(parent, obj._index))
        obj._linear_functions = [f for f in self._linear_funcs_rule(
            parent, obj._index)]
        return obj

    def _getitem_when_not_present(self, index):
        if index is None and not self.is_indexed():
            obj = self._data[index] = self
        else:
            obj = self._data[index] = self._ComponentDataClass(component=self)
        obj._index = index
        parent = obj.parent_block()

        # Get the nonlinear function, if we have one.
        nonlinear_function = None
        if self._func_rule is not None:
            nonlinear_function = self._func_rule(parent, index)
        elif self._func is not None:
            nonlinear_function = self._func

        handler = self._handlers.get((nonlinear_function is not None,
                                      self._points_rule is not None,
                                      self._simplices_rule is not None,
                                      self._linear_funcs_rule is not None))
        if handler is None:
            raise ValueError("Unsupported set of arguments given for "
                             "constructing PiecewiseLinearFunction. "
                             "Expected a nonlinear function and a list"
                             "of breakpoints, a nonlinear function and a list "
                             "of simplices, or a list of linear functions and "
                             "a list of corresponding simplices.")
        return handler(obj, parent, nonlinear_function)


class ScalarPiecewiseLinearFunction(PiecewiseLinearFunctionData,
                                    PiecewiseLinearFunction):
    def __init__(self, *args, **kwds):
        self._suppress_ctypes = set()

        PiecewiseLinearFunctionData.__init__(self, self)
        PiecewiseLinearFunction.__init__(self, *args, **kwds)
        self._data[None] = self
        self._index = UnindexedComponent_index

class IndexedPiecewiseLinearFunction(PiecewiseLinearFunction):
    pass

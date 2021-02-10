#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright 2017 National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

"""Utility functions and classes for the MindtPy solver."""
from __future__ import division
import logging
from pyomo.common.collections import ComponentMap
from pyomo.core import (Block, Constraint,
                        Objective, Reals, Suffix, Var, minimize, maximize, RangeSet, ConstraintList, TransformationFactory)
from pyomo.core.expr import differentiate
from pyomo.core.expr import current as EXPR
from pyomo.opt import SolverFactory
from pyomo.solvers.plugins.solvers.persistent_solver import PersistentSolver
from pyomo.contrib.pynumero.interfaces.pyomo_nlp import PyomoNLP
from pyomo.contrib.gdpopt.util import copy_var_list_values, get_main_elapsed_time, time_code
import numpy as np
from pyomo.core.expr.taylor_series import taylor_series_expansion
from pyomo.core.expr.calculus.derivatives import differentiate

logger = logging.getLogger('pyomo.contrib')


class MindtPySolveData(object):
    """Data container to hold solve-instance data.
    Key attributes:
        - original_model: the original model that the user gave us to solve
        - working_model: the original model after preprocessing
    """
    pass


def model_is_valid(solve_data, config):
    """
    Determines whether the model is solveable by MindtPy.

    This function returns True if the given model is solveable by MindtPy (and performs some preprocessing such
    as moving the objective to the constraints).

    Parameters
    ----------
    solve_data: MindtPy Data Container
        data container that holds solve-instance data
    config: MindtPy configurations
        contains the specific configurations for the algorithm

    Returns
    -------
    Boolean value (True if model is solveable in MindtPy else False)
    """
    m = solve_data.working_model
    MindtPy = m.MindtPy_utils

    # Handle LP/NLP being passed to the solver
    prob = solve_data.results.problem
    if len(MindtPy.discrete_variable_list) == 0:
        config.logger.info('Problem has no discrete decisions.')
        obj = next(m.component_data_objects(ctype=Objective, active=True))
        if (any(c.body.polynomial_degree() not in (1, 0) for c in MindtPy.constraint_list) or
                obj.expr.polynomial_degree() not in (1, 0)):
            config.logger.info(
                'Your model is a NLP (nonlinear program). '
                'Using NLP solver %s to solve.' % config.nlp_solver)
            nlpopt = SolverFactory(config.nlp_solver)
            set_solver_options(nlpopt, solve_data, config, solver_type='nlp')
            nlpopt.solve(
                solve_data.original_model, tee=config.nlp_solver_tee, **config.nlp_solver_args)
            return False
        else:
            config.logger.info(
                'Your model is an LP (linear program). '
                'Using LP solver %s to solve.' % config.mip_solver)
            masteropt = SolverFactory(config.mip_solver)
            if isinstance(masteropt, PersistentSolver):
                masteropt.set_instance(solve_data.original_model)
            set_solver_options(masteropt, solve_data,
                               config, solver_type='mip')
            masteropt.solve(solve_data.original_model,
                            tee=config.mip_solver_tee, **config.mip_solver_args)
            return False

    if not hasattr(m, 'dual') and config.calculate_dual:  # Set up dual value reporting
        m.dual = Suffix(direction=Suffix.IMPORT)

    # TODO if any continuous variables are multiplied with binary ones,
    #  need to do some kind of transformation (Glover?) or throw an error message
    return True


def calc_jacobians(solve_data, config):
    """
    Generates a map of jacobians for the variables in the model

    This function generates a map of jacobians corresponding to the variables in the model and adds this
    ComponentMap to solve_data

    Parameters
    ----------
    solve_data: MindtPy Data Container
        data container that holds solve-instance data
    config: MindtPy configurations
        contains the specific configurations for the algorithm
    """
    # Map nonlinear_constraint --> Map(
    #     variable --> jacobian of constraint wrt. variable)
    solve_data.jacobians = ComponentMap()
    if config.differentiate_mode == 'reverse_symbolic':
        mode = differentiate.Modes.reverse_symbolic
    elif config.differentiate_mode == 'sympy':
        mode = differentiate.Modes.sympy
    for c in solve_data.mip.MindtPy_utils.nonlinear_constraint_list:
        vars_in_constr = list(EXPR.identify_variables(c.body))
        jac_list = differentiate(
            c.body, wrt_list=vars_in_constr, mode=mode)
        solve_data.jacobians[c] = ComponentMap(
            (var, jac_wrt_var)
            for var, jac_wrt_var in zip(vars_in_constr, jac_list))


def add_feas_slacks(m, config):
    """
    Adds feasibility slack variables according to config.feasibility_norm (given an infeasible problem)

    Parameters
    ----------
    m: model
        Pyomo model
    config: ConfigBlock
        contains the specific configurations for the algorithm
    """
    MindtPy = m.MindtPy_utils
    # generate new constraints
    for i, constr in enumerate(MindtPy.nonlinear_constraint_list, 1):
        if constr.has_ub():
            if config.feasibility_norm in {'L1', 'L2'}:
                MindtPy.feas_opt.feas_constraints.add(
                    constr.body - constr.upper
                    <= MindtPy.feas_opt.slack_var[i])
            else:
                MindtPy.feas_opt.feas_constraints.add(
                    constr.body - constr.upper
                    <= MindtPy.feas_opt.slack_var)
        if constr.has_lb():
            if config.feasibility_norm in {'L1', 'L2'}:
                MindtPy.feas_opt.feas_constraints.add(
                    constr.body - constr.lower
                    >= -MindtPy.feas_opt.slack_var[i])
            else:
                MindtPy.feas_opt.feas_constraints.add(
                    constr.body - constr.lower
                    >= -MindtPy.feas_opt.slack_var)


def var_bound_add(solve_data, config):
    """
    This function will add bounds for variables in nonlinear constraints if they are not bounded. (This is to avoid
    an unbounded master problem in the LP/NLP algorithm.) Thus, the model will be updated to include bounds for the
    unbounded variables in nonlinear constraints.

    Parameters
    ----------
    solve_data: MindtPy Data Container
        data container that holds solve-instance data
    config: ConfigBlock
        contains the specific configurations for the algorithm

    """
    m = solve_data.working_model
    MindtPy = m.MindtPy_utils
    for c in MindtPy.nonlinear_constraint_list:
        for var in list(EXPR.identify_variables(c.body)):
            if var.has_lb() and var.has_ub():
                continue
            elif not var.has_lb():
                if var.is_integer():
                    var.setlb(-config.integer_var_bound - 1)
                else:
                    var.setlb(-config.continuous_var_bound - 1)
            elif not var.has_ub():
                if var.is_integer():
                    var.setub(config.integer_var_bound)
                else:
                    var.setub(config.continuous_var_bound)


def generate_norm2sq_objective_function(model, setpoint_model, discrete_only=False):
    """
    This function generates objective (FP-NLP subproblem) for minimum euclidean distance to setpoint_model
    L2 distance of (x,y) = \sqrt{\sum_i (x_i - y_i)^2}

    Parameters
    ----------
    model: Pyomo model
        the model that needs new objective function
    setpoint_model: Pyomo model
        the model that provides the base point for us to calculate the distance
    discrete_only: Bool
        only optimize on distance between the discrete variables
    """
    # skip objective_value variable and slack_var variables
    var_filter = (lambda v: v[1].is_integer()) if discrete_only \
        else (lambda v: v[1].name != 'MindtPy_utils.objective_value' and
              'MindtPy_utils.feas_opt.slack_var' not in v[1].name)

    model_vars, setpoint_vars = zip(*filter(var_filter,
                                            zip(model.component_data_objects(Var),
                                                setpoint_model.component_data_objects(Var))))
    assert len(model_vars) == len(
        setpoint_vars), 'Trying to generate Squared Norm2 objective function for models with different number of variables'

    return Objective(expr=(
        sum([(model_var - setpoint_var.value)**2
             for (model_var, setpoint_var) in
             zip(model_vars, setpoint_vars)])))


def generate_norm1_objective_function(model, setpoint_model, discrete_only=False):
    """
    This function generates objective (PF-OA master problem) for minimum Norm1 distance to setpoint_model
    Norm1 distance of (x,y) = \sum_i |x_i - y_i|

    Parameters
    ----------
    model: Pyomo model
        the model that needs new objective function
    setpoint_model: Pyomo model
        the model that provides the base point for us to calculate the distance
    discrete_only: Bool
        only optimize on distance between the discrete variables
    """
    # skip objective_value variable and slack_var variables
    var_filter = (lambda v: v.is_integer()) if discrete_only \
        else (lambda v: v.name != 'MindtPy_utils.objective_value' and
              'MindtPy_utils.feas_opt.slack_var' not in v.name)
    model_vars = list(filter(var_filter, model.component_data_objects(Var)))
    setpoint_vars = list(
        filter(var_filter, setpoint_model.component_data_objects(Var)))
    assert len(model_vars) == len(
        setpoint_vars), 'Trying to generate Norm1 objective function for models with different number of variables'
    model.MindtPy_utils.del_component('L1_obj')
    obj_blk = model.MindtPy_utils.L1_obj = Block()
    obj_blk.L1_obj_idx = RangeSet(len(model_vars))
    obj_blk.L1_obj_var = Var(
        obj_blk.L1_obj_idx, domain=Reals, bounds=(0, None))
    obj_blk.abs_reform = ConstraintList()
    for idx, v_model, v_setpoint in zip(obj_blk.L1_obj_idx, model_vars,
                                        setpoint_vars):
        obj_blk.abs_reform.add(
            expr=v_model - v_setpoint.value >= -obj_blk.L1_obj_var[idx])
        obj_blk.abs_reform.add(
            expr=v_model - v_setpoint.value <= obj_blk.L1_obj_var[idx])

    return Objective(expr=sum(obj_blk.L1_obj_var[idx] for idx in obj_blk.L1_obj_idx))


def generate_norm_inf_objective_function(model, setpoint_model, discrete_only=False):
    """
    This function generates objective (PF-OA master problem) for minimum Norm Infinity distance to setpoint_model
    Norm-Infinity distance of (x,y) = \max_i |x_i - y_i|

    Parameters
    ----------
    model: Pyomo model
        the model that needs new objective function
    setpoint_model: Pyomo model
        the model that provides the base point for us to calculate the distance
    discrete_only: Bool
        only optimize on distance between the discrete variables
    """
    # skip objective_value variable and slack_var variables
    var_filter = (lambda v: v.is_integer()) if discrete_only \
        else (lambda v: v.name != 'MindtPy_utils.objective_value' and
              'MindtPy_utils.feas_opt.slack_var' not in v.name)
    model_vars = list(filter(var_filter, model.component_data_objects(Var)))
    setpoint_vars = list(
        filter(var_filter, setpoint_model.component_data_objects(Var)))
    assert len(model_vars) == len(
        setpoint_vars), 'Trying to generate Norm Infinity objective function for models with different number of variables'
    model.MindtPy_utils.del_component('L_infinity_obj')
    obj_blk = model.MindtPy_utils.L_infinity_obj = Block()
    obj_blk.L_infinity_obj_var = Var(domain=Reals, bounds=(0, None))
    obj_blk.abs_reform = ConstraintList()
    for v_model, v_setpoint in zip(model_vars,
                                   setpoint_vars):
        obj_blk.abs_reform.add(
            expr=v_model - v_setpoint.value >= -obj_blk.L_infinity_obj_var)
        obj_blk.abs_reform.add(
            expr=v_model - v_setpoint.value <= obj_blk.L_infinity_obj_var)

    return Objective(expr=obj_blk.L_infinity_obj_var)


def generate_lag_objective_function(model, setpoint_model, config, solve_data, discrete_only=False):
    """The function generate taylor extension of the Lagrangean function.

    Args:
        model ([type]): [description]
        setpoint_model ([type]): [description]
        discrete_only (bool, optional): [description]. Defaults to False.
    """
    temp_model = setpoint_model.clone()
    for var in temp_model.MindtPy_utils.variable_list:
        if var.is_integer():
            var.unfix()
    # objective_list[0] is the original objective function, not in MindtPy_utils block
    temp_model.MindtPy_utils.objective_list[0].activate()
    temp_model.MindtPy_utils.deactivate()
    TransformationFactory('core.relax_integer_vars').apply_to(temp_model)
    # Note: PyNumero does not support discrete variables
    # So PyomoNLP should operate on setpoint_model

    # Implementation 1
    # First calculate jacobin and hessian without assigning variable and constraint sequence, then use get_primal_indices to get the indices.
    with time_code(solve_data.timing, 'PyomoNLP'):
        nlp = PyomoNLP(temp_model)
        lam = [-temp_model.dual[constr] if abs(temp_model.dual[constr]) > config.zero_tolerance else 0
               for constr in nlp.get_pyomo_constraints()]
        nlp.set_duals(lam)
        obj_grad = nlp.evaluate_grad_objective().reshape(-1, 1)
        jac = nlp.evaluate_jacobian().toarray()
        jac_lag = obj_grad + jac.transpose().dot(np.array(lam).reshape(-1, 1))
        jac_lag[abs(jac_lag) < config.zero_tolerance] = 0
        nlp_var = set([i.name for i in nlp.get_pyomo_variables()])
        first_order_term = sum(float(jac_lag[nlp.get_primal_indices([temp_var])[0]]) * (var - temp_var.value) for var,
                               temp_var in zip(model.MindtPy_utils.variable_list[:-1], temp_model.MindtPy_utils.variable_list[:-1]) if temp_var.name in nlp_var)

        if config.add_regularization == 'grad_lag':
            return Objective(expr=first_order_term, sense=minimize)
        elif config.add_regularization == 'hess_lag':
            # Implementation 1
            hess_lag = nlp.evaluate_hessian_lag().toarray()
            hess_lag[abs(hess_lag) < config.zero_tolerance] = 0
            second_order_term = 0.5 * sum((var_i - temp_var_i.value) * float(hess_lag[nlp.get_primal_indices([temp_var_i])[0]][nlp.get_primal_indices([temp_var_j])[0]]) * (var_j - temp_var_j.value)
                                          for var_i, temp_var_i in zip(model.MindtPy_utils.variable_list[:-1], temp_model.MindtPy_utils.variable_list[:-1])
                                          for var_j, temp_var_j in zip(model.MindtPy_utils.variable_list[:-1], temp_model.MindtPy_utils.variable_list[:-1])
                                          if (temp_var_i.name in nlp_var and temp_var_j.name in nlp_var))
            return Objective(expr=first_order_term + second_order_term, sense=minimize)


def generate_norm1_norm_constraint(model, setpoint_model, config, discrete_only=True):
    """
    This function generates constraint (PF-OA master problem) for minimum Norm1 distance to setpoint_model
    Norm constraint is used to guarantees the monotonicity of the norm objective value sequence of all iterations
    Norm1 distance of (x,y) = \sum_i |x_i - y_i|
    Ref: Paper 'A storm of feasibility pumps for nonconvex MINLP' Eq. (16)

    Parameters
    ----------
    model: Pyomo model
        the model that needs new objective function
    setpoint_model: Pyomo model
        the model that provides the base point for us to calculate the distance
    discrete_only: Bool
        only optimize on distance between the discrete variables
    """

    var_filter = (lambda v: v.is_integer()) if discrete_only \
        else (lambda v: True)
    model_vars = list(filter(var_filter, model.component_data_objects(Var)))
    setpoint_vars = list(
        filter(var_filter, setpoint_model.component_data_objects(Var)))
    assert len(model_vars) == len(
        setpoint_vars), 'Trying to generate Norm1 norm constraint for models with different number of variables'
    norm_constraint_blk = model.MindtPy_utils.L1_norm_constraint = Block()
    norm_constraint_blk.L1_slack_idx = RangeSet(len(model_vars))
    norm_constraint_blk.L1_slack_var = Var(
        norm_constraint_blk.L1_slack_idx, domain=Reals, bounds=(0, None))
    norm_constraint_blk.abs_reform = ConstraintList()
    for idx, v_model, v_setpoint in zip(norm_constraint_blk.L1_slack_idx, model_vars,
                                        setpoint_vars):
        norm_constraint_blk.abs_reform.add(
            expr=v_model - v_setpoint.value >= -norm_constraint_blk.L1_slack_var[idx])
        norm_constraint_blk.abs_reform.add(
            expr=v_model - v_setpoint.value <= norm_constraint_blk.L1_slack_var[idx])
    rhs = config.fp_norm_constraint_coef * \
        sum(abs(v_model.value-v_setpoint.value)
            for v_model, v_setpoint in zip(model_vars, setpoint_vars))
    norm_constraint_blk.sum_slack = Constraint(
        expr=sum(norm_constraint_blk.L1_slack_var[idx] for idx in norm_constraint_blk.L1_slack_idx) <= rhs)


def set_solver_options(opt, solve_data, config, solver_type, regularization=False):
    """ set options for MIP/NLP solvers

    Args:
        opt : SolverFactory
            the solver
        solve_data: MindtPy Data Container
            data container that holds solve-instance data
        config: ConfigBlock
            contains the specific configurations for the algorithm
        solver_type: String
            The type of the solver, i.e. mip or nlp
        regularization (bool, optional): Boolean. 
            Defaults to False.
    """
    # TODO: integrate nlp_args here
    # nlp_args = dict(config.nlp_solver_args)
    elapsed = get_main_elapsed_time(solve_data.timing)
    remaining = int(max(config.time_limit - elapsed, 1))
    if solver_type == 'mip':
        solver_name = config.mip_solver
        if regularization:
            if config.projection_mip_threads > 0:
                opt.options['threads'] = config.projection_mip_threads
        else:
            if config.threads > 0:
                opt.options['threads'] = config.threads
    elif solver_type == 'nlp':
        solver_name = config.nlp_solver
    # TODO: opt.name doesn't work for GAMS
    if solver_name in {'cplex', 'gurobi', 'gurobi_persistent'}:
        opt.options['timelimit'] = remaining
        opt.options['mipgap'] = config.mip_solver_mipgap
        if regularization == True:
            if solver_name == 'cplex':
                opt.options['mip limits populate'] = config.solution_limit
                opt.options['mip strategy presolvenode'] = 3
                # TODO: need to discuss if this option should be added.
                # if config.add_regularization == 'hess_lag':
                #     opt.options['optimalitytarget'] = 3
            elif solver_name == 'gurobi':
                opt.options['SolutionLimit'] = config.solution_limit
                opt.options['Presolve'] = 2
    elif solver_name == 'cplex_persistent':
        opt.options['timelimit'] = remaining
        opt._solver_model.parameters.mip.tolerances.mipgap.set(
            config.mip_solver_mipgap)
        if regularization == True:
            opt._solver_model.parameters.mip.limits.populate.set(
                config.solution_limit)
            opt._solver_model.parameters.mip.strategy.presolvenode.set(3)
            if config.add_regularization == 'hess_lag':
                opt._solver_model.parameters.optimalitytarget.set(3)
    elif solver_name == 'glpk':
        opt.options['tmlim'] = remaining
        # TODO: mipgap does not work for glpk yet
        # opt.options['mipgap'] = config.mip_solver_mipgap
    elif solver_name == 'baron':
        opt.options['MaxTime'] = remaining
        opt.options['AbsConFeasTol'] = config.zero_tolerance
    elif solver_name == 'ipopt':
        opt.options['max_cpu_time'] = remaining
        opt.options['constr_viol_tol'] = config.zero_tolerance
    elif solver_name == 'gams':
        if solver_type == 'mip':
            opt.options['add_options'] = ['option optcr=%s;' % config.mip_solver_mipgap,
                                          'option reslim=%s;' % remaining]
        elif solver_type == 'nlp':
            opt.options['add_options'] = ['option reslim=%s;' % remaining]
            if config.nlp_solver_args.__contains__('solver'):
                if config.nlp_solver_args['solver'] in {'ipopt', 'ipopth', 'msnlp', 'conopt', 'baron'}:
                    if config.nlp_solver_args['solver'] == 'ipopt':
                        opt.options['add_options'].append(
                            '$onecho > ipopt.opt')
                        opt.options['add_options'].append(
                            'constr_viol_tol ' + str(config.zero_tolerance))
                    elif config.nlp_solver_args['solver'] == 'ipopth':
                        opt.options['add_options'].append(
                            '$onecho > ipopth.opt')
                        opt.options['add_options'].append(
                            'constr_viol_tol ' + str(config.zero_tolerance))
                    elif config.nlp_solver_args['solver'] == 'conopt':
                        opt.options['add_options'].append(
                            '$onecho > conopt.opt')
                        opt.options['add_options'].append(
                            'RTNWMA ' + str(config.zero_tolerance))
                    elif config.nlp_solver_args['solver'] == 'msnlp':
                        opt.options['add_options'].append(
                            '$onecho > msnlp.opt')
                        opt.options['add_options'].append(
                            'feasibility_tolerance ' + str(config.zero_tolerance))
                    elif config.nlp_solver_args['solver'] == 'baron':
                        opt.options['add_options'].append(
                            '$onecho > baron.opt')
                        opt.options['add_options'].append(
                            'AbsConFeasTol ' + str(config.zero_tolerance))
                    opt.options['add_options'].append('$offecho')
                    opt.options['add_options'].append('GAMS_MODEL.optfile=1')

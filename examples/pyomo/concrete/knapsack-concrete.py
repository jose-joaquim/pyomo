#!/usr/bin/env python
# ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright (c) 2008-2024
#  National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

#
# Knapsack Problem
#

from pyomo.environ import *

v = {"hammer": 8, "wrench": 3, "screwdriver": 6, "towel": 11}
w = {"hammer": 5, "wrench": 7, "screwdriver": 4, "towel": 3}
# v = {"hammer": 8}
# w = {"hammer": 5}

limit = 14


M = ConcreteModel()

M.ITEMS = Set(initialize=v.keys())

M.x = Var(M.ITEMS, within=Binary)
# M.x.set_scen_lb(1, 0)
# M.x["hammer"].ub = 0
tmp = M.x["hammer"].scen_lb
tmp[1] = 1

M.value = Objective(expr=sum(v[i] * M.x[i] for i in M.ITEMS), sense=maximize)

breakpoint()
M.weight = Constraint(expr=sum(w[i] * M.x[i] for i in M.ITEMS) <= limit)
M.weight.upper = 18

M.write("seila.lp", io_options={"symbolic_solver_labels": True})

with SolverFactory(
    "gurobi_direct",
    options={"MIPGap": 0.05, "NumScenarios": 2, "LogToConsole": 1},
) as opt:
    breakpoint()
    rst = opt.solve(M, tee=True)

    # tmp = opt.get_solver_model()
    # tmp = opt._solver_model
    # tmp.reset()
    # seila = tmp.addVar(lb=2, ub=27, obj=10, name="testando")
    #
    # tmp.write("from_gurobi.lp")
    # tmp.optimize()
    #
    # print("seilaaaaaaaa! ", tmp.gurobi.version())

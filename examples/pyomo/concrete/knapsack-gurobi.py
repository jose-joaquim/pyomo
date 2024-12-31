from pyomo.environ import (
    Constraint,
    Var,
    ConcreteModel,
    Binary,
    quicksum,
    SolverFactory,
    Objective,
    minimize,
    NonNegativeReals,
)

# Warehouse demand in thousands of units
demand = [15, 18, 14, 20]

# Plant capacity in thousands of units
capacity = [20, 22, 17, 19, 18]

# Fixed costs for each plant
fixedCosts = [12000, 15000, 17000, 13000, 16000]
maxFixed = max(fixedCosts)
minFixed = min(fixedCosts)

# Transportation costs per thousand units
transCosts = [
    [4000, 2000, 3000, 2500, 4500],
    [2500, 2600, 3400, 3000, 4000],
    [1200, 1800, 2600, 4100, 3000],
    [2200, 2600, 3100, 3700, 3200],
]

# Range of plants and warehouses
plants = range(len(capacity))
warehouses = range(len(demand))

# Model
M = ConcreteModel()

# Plant open decision variables: open[p] == 1 if plant p is open.
M.open = Var(plants, within=Binary)

# Transportation decision variables: transport[w,p] captures the
# optimal quantity to transport to warehouse w from plant p
M.transport = Var(warehouses, plants, name="trans", within=NonNegativeReals)

# Production constraints
# Note that the right-hand limit sets the production to zero if the plant
# is closed
M.capacity = Constraint(
    plants,
    rule=lambda _, p: quicksum(M.transport[k, p] for k in warehouses)
    <= capacity[p] * M.open[p],
    name="trans",
)

M.demand = Constraint(
    warehouses,
    rule=lambda _, w: quicksum(M.transport[w, p] for p in plants) == demand[w],
    name="demand",
)

M.of = Objective(
    expr=quicksum(M.open[p] * fixedCosts[p] for p in plants)
    + quicksum(
        M.transport[t, w] * transCosts[t][w]
        for t in range(len(transCosts))
        for w in range(len(transCosts[t]))
    ),
    sense=minimize,
)

# We constructed the base model, now we add 7 scenarios
#
# Scenario 0: Represents the base model, hence, no manipulations.
# Scenario 1: Manipulate the warehouses demands slightly (constraint right
#             hand sides).
# Scenario 2: Double the warehouses demands (constraint right hand sides).
# Scenario 3: Manipulate the plant fixed costs (objective coefficients).
# Scenario 4: Manipulate the warehouses demands and fixed costs.
# Scenario 5: Force the plant with the largest fixed cost to stay open
#             (variable bounds).
# Scenario 6: Force the plant with the smallest fixed cost to be closed
#             (variable bounds).

# # Scenario 0: Base model, hence, nothing to do except giving the scenario a
# #             name
# m.Params.ScenarioNumber = 0
# m.ScenNName = "Base model"

# # Scenario 1: Increase the warehouse demands by 10%
# m.Params.ScenarioNumber = 1
# m.ScenNName = "Increased warehouse demands"
# for w in warehouses:
#     demandConstr[w].ScenNRhs = demand[w] * 1.1
#
# # Scenario 2: Double the warehouse demands
# m.Params.ScenarioNumber = 2
# m.ScenNName = "Double the warehouse demands"
# for w in warehouses:
#     demandConstr[w].ScenNRhs = demand[w] * 2.0
#
# # Scenario 3: Decrease the plant fixed costs by 5%
# m.Params.ScenarioNumber = 3
# m.ScenNName = "Decreased plant fixed costs"
# for p in plants:
#     open[p].ScenNObj = fixedCosts[p] * 0.95
#
# # Scenario 4: Combine scenario 1 and scenario 3
# m.Params.ScenarioNumber = 4
# m.ScenNName = "Increased warehouse demands and decreased plant fixed costs"
# for w in warehouses:
#     demandConstr[w].ScenNRhs = demand[w] * 1.1
# for p in plants:
#     open[p].ScenNObj = fixedCosts[p] * 0.95

# Scenario 5: Force the plant with the largest fixed cost to stay open
# m.Params.ScenarioNumber = 5
# m.ScenNName = "Force plant with largest fixed cost to stay open"
M.open[fixedCosts.index(maxFixed)].scen_lb[1] = 1.0

# Scenario 6: Force the plant with the smallest fixed cost to be closed
# m.Params.ScenarioNumber = 6
# m.ScenNName = "Force plant with smallest fixed cost to be closed"
M.open[fixedCosts.index(minFixed)].scen_ub[2] = 0.0

breakpoint()
with SolverFactory("gurobi_direct", options={"NumScenarios": 3}) as opt:
    rst = opt.solve(M, tee=True)

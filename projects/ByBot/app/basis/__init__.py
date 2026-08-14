"""Dated-futures basis observation.

Unlike every strategy in app/backtest, this measures a payoff that is
contractual rather than predicted: a dated future must converge to the index
at delivery, so the premium is a decay schedule, not a forecast. That is why
it is an observation tool and not a backtested strategy — there is no
hypothesis here to reject, only a rate to watch and a cost to clear.

Read-only. It has no order path and no exchange mutation method.
"""

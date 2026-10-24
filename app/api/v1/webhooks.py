"""Aggregator payment callbacks. Lands with the phase that adds an aggregator.

Deliberately empty for now. Phase 1 settles fees through the audited manual
entry in `app/api/v1/payments.py`, which is the school's actual process: a
bursar holds an M-Pesa confirmation or a bank slip and types its receipt.

What was here was a route sketch, and it is better gone than half-wired. A
public endpoint that records money needs a provider that can sign a callback,
and `app/integrations/payments/mock.py` is still a placeholder — so the
endpoint would have had to either trust an unsigned POST or reject every
request. The service it would call is written and tested already
(`app/services/payments.py`, `app/domain/payments.decide`), so what remains for
that phase is a provider adapter, a school lookup that works without a
subdomain header, and this route.
"""

"""coordination.infra — machinery owned by no single topology.

Everything the five collaboration styles share, none of their semantics:

- ``stage``       the stage lifecycle: dispatch (serial / concurrent /
                   single_winner), termination policy, the budget envelope,
                   and the restore-or-fresh decision (``has_durable_events``).
- ``store``       the shared-state contract (``SharedStore``) plus
                   ``EventSourcedStore`` — record-and-advance mechanics every
                   durable store reuses; event types, reducers and ``restore``
                   stay in their own domains.
- ``stage_tools`` turn a *named* spec into a model-callable tool
                   (``run_blackboard``).
- ``settle``      chain-terminal discipline — structured output, the root's
                   output contract, checkpoint, RUN_COMPLETE gate — for any
                   chain, not just peer chains.

The message plane lives beside this package as ``coordination.messaging``:
infra is the machinery *around* the loops, messaging is the plane results
cross — two different axes, kept separate on purpose.
"""

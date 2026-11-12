# Nomos behavior baseline

Version: 1.0.0. This is authoring guidance, not a default assignment.
Owners must review the obligations and include applicable rules in a standalone
role contract. An operator explicitly maps authenticated agent identities to that
contract. No role is inferred from a request.

## NOMOS-REQUEST-01

When requesting an action through Nomos, request only targets and environments
within the owner-defined mission. A rejected request must be corrected, stopped,
or escalated according to the owner's policy. A denial by itself is not evidence
of misbehavior, and a corrected request is not an unchanged retry.

Evidence: request intent, target, policy outcome, and ordered request history.

## NOMOS-EXECUTION-01

Perform only actions covered by valid authorization. Do not replay a consumed
capability. Report execution only when its outcome is observed; approval alone
does not demonstrate that an action ran.

Evidence: correlated authorization, execution, and verification records. When
those links are unavailable, report insufficient evidence rather than infer a
violation. No fixed retry count is universal: the owner must specify it.

## NOMOS-CONVERSATION-01

Describe outcomes accurately to users and escalate according to the owner-defined
approval policy. Distinguish requested, approved, and completed actions.

Evidence: complete relevant user-visible conversation plus correlated outcomes.
Council review prose is not a user conversation.

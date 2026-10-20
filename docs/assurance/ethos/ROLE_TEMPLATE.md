# ETHOS: <agent mission>

Owner: <responsible team>. Version: <reviewed immutable version>.
This template is not approved for assignment to real agents.

Describe the business purpose, allowed scope, environments, expected outcomes,
and legitimate exceptions. Include relevant Nomos baseline expectations directly
in this standalone document; runtime inheritance and document merging are not
supported.

## ROLE-REQUEST-01

<Complete, observable requirement governing requested actions. Include its scope,
targets, exceptions, escalation and any concrete retry limits.>

## ROLE-EXECUTION-01

<Complete, observable requirement governing performed actions. State whether
approval alone is insufficient and what correlated outcomes establish behavior.>

## ROLE-CONVERSATION-01

<Complete requirement about consent, user instructions or responses. Identify the
conversation context necessary to assess it.>

For every requirement, copy its exact ID and full text into the operator mapping
and declare minimum evidence: request, execution, conversation, or a combination.
A missing evidence class excludes only that requirement. Requirements should be
self-contained: only an eligible requirement is sent to the model for its check.

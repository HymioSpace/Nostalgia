# Context-originated task opportunities

Status: Implemented

## Overview

Dreaming extensions may suggest follow-up work grounded in the frozen source window. Nostalgia
validates evidence and commits suggestions with the context version. A durable delivery outbox
retries publication independently of model inference; failed delivery never rolls back published
context or reruns the model just to resend a suggestion.

## Boundaries

The runtime remains application-neutral: suggestions carry stable IDs, evidence and proposed work,
not application accounts or execution authority. Hymio decides whether an opportunity is appropriate,
requires user input or duplicates existing work. Administrative status and data use existing bearer
identity; every query is scoped to its authenticated context owner.

Publication uses the existing owner-scoped context event log as its durable outbox. The Hymio
consumer maintains its own delivery cursor; acknowledgement follows durable downstream acceptance.
At most three proposals are accepted per dream, each referencing original IDs in the frozen input.
Unfounded proposals abort the draft atomically. `/v1/operations/status` returns bounded recent
lifecycle events and owner-scoped counts, without tokens or provider request content.

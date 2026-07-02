---
title: Zephyrcache invalidation must be write-through
type: pattern
tags: ["zephyrcache", "caching"]
source: fixture
certainty: 3
date: {{DAYS_AGO_2}}
---

Zephyrcache invalidation works reliably only as write-through.
Lazy invalidation loses updates under concurrent writers.

## Related

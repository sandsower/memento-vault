<!-- beislid-workflow: v1 -->

# Beislið workflow config — memento-vault

## Issue tracker

GitHub Issues on `sandsower/memento-vault`, accessed via the `gh` CLI.

```beislid:ticket_source
type: cli
command: 'gh issue view {id} --json title,body,labels'
id_pattern: '^#?\d+$'
link_template: 'https://github.com/sandsower/memento-vault/issues/{id}'
```

## Probe cache

```beislid:probe_cache
ttl_hours: 24
```

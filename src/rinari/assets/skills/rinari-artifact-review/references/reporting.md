# Reporting a review

## Severity

| Severity | Meaning | Blocks delivery |
|---|---|---|
| `critical` | Wrong or misleading content, data loss, broken file | Yes |
| `error` | Clearly visible defect: cut text, unreadable chart, broken layout | Yes |
| `warning` | Noticeable quality problem a careful reader would see | No |
| `suggestion` | Improvement, not a defect | No |

## Finding

```json
{"severity": "error", "page": 4, "shape_id": 7, "category": "design",
 "message": "Chart labels overlap for Centro and Sur", "fix": "Use bar instead of column or shorten labels"}
```

`category` is one of content, data, preservation, design or accessibility. One finding per problem, located as precisely as the evidence allows.

## Report to the requester

1. Revision id and sha (first 8 characters), renderer and pages reviewed of the total.
2. Status per dimension as `documents.validate` returns it.
3. Findings ordered by severity, each with its location and fix.
4. What was not reviewed and why.

Never write "verified" or "approved" without the scope next to it.

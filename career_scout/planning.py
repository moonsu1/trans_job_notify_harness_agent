"""Keep all four financial sectors in the search plan without increasing the call cap."""
import math


def search_plan(companies, full_scan=False, max_calls=12):
    if full_scan:
        return [([c], track) for c in companies for track in ('engineering', 'planning')]
    plan = []
    for tier in (1, 2):
        group = [c for c in companies if c['tier'] == tier]
        if group:
            plan.extend((group, track) for track in ('engineering', 'planning'))
    for category in ('securities', 'card', 'bank', 'insurance'):
        group = [c for c in companies if c.get('finance_category') == category]
        if group:
            plan.append((group, 'combined'))
    others = [c for c in companies if c['tier'] == 3 and not c.get('finance_category')]
    available_groups = max(1, (max_calls-len(plan))//2)
    size = max(1, math.ceil(len(others)/available_groups))
    for start in range(0, len(others), size):
        plan.extend((others[start:start+size], track) for track in ('engineering', 'planning'))
    return plan

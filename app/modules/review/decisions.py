from collections import defaultdict
from hashlib import sha256

from app.manifest import canonical_json
from app.modules.review.coverage import assess

SIGNABLE = ('READY', 'LIMITED')


def _accepted_by_sheet(refit):
    grouped = defaultdict(list)
    for row in refit['accepted']:
        grouped[row['sheet']].append(row)
    return grouped


def decisions(catalog, refit, seams):
    incident = defaultdict(list)
    for edge in seams['edges']:
        incident[edge['a']].append(edge)
        incident[edge['b']].append(edge)
    accepted = _accepted_by_sheet(refit)
    reviews = []
    for sheet in catalog['sheets']:
        code = sheet['code']
        model, edges = refit['sheets'][code], incident[code]
        coverage = assess(sheet, accepted[code])
        failed = [edge['code'] for edge in edges if edge['status'] == 'FAIL']
        blocked = [edge['code'] for edge in edges if edge['status'] == 'BLOCKED']
        scope = coverage['scope']
        if model['status'] != 'FIT':
            status, reason, scope = 'HOLD', model['reason'], None
        elif failed:
            status, reason, scope = 'RETURN', 'GAP_OVER_LIMIT', None
        elif coverage['verdict'] == 'RETURN':
            status, reason, scope = 'RETURN', coverage['reason'], None
        elif blocked:
            status, reason, scope = 'HOLD', next(edge['reason'] for edge in edges if edge['status'] == 'BLOCKED'), None
        elif not edges:
            status, reason, scope = 'HOLD', 'NO_EDGE_REGISTERED', None
        elif coverage['verdict'] == 'LIMITED':
            status, reason = 'LIMITED', None
        else:
            status, reason = 'READY', None
        reviews.append({**sheet, 'status': status, 'reason': reason, 'fit_rms_m': model.get('rms_m'),
                        'edges': [edge['code'] for edge in edges], 'failed_edges': failed,
                        'blocked_edges': blocked, 'coverage': coverage, 'scope': scope,
                        'signable': status in SIGNABLE})
    return reviews


def decision_digest(reviews, refit=None, seams=None):
    """裁定摘要：逐幅裁定加上采信/离群控制点与拼缝证据，任一变动都会改摘要。"""
    payload = {'reviews': reviews}
    if refit is not None:
        payload['controls'] = {
            'accepted': sorted((row['sheet'], row['id']) for row in refit['accepted']),
            'outliers': sorted((row['sheet'], row['id']) for row in refit['outliers']),
        }
    if seams is not None:
        payload['edges'] = [(edge['code'], edge['status'], edge.get('max_m')) for edge in seams['edges']]
    return sha256(canonical_json(payload).encode()).hexdigest()

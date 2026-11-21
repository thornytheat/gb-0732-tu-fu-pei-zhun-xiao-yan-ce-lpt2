"""逐幅控制点覆盖裁定。

拟合误差只能说明模型对已采信点贴合得好，不能说明图框四角都有证据。
本模块用采信控制点的凸包（像素域）逐角量距，给出整幅放行、限定范围或退回
的裁定，并把可签范围固定下来，供签审逐字核对。

规则（与 docs/DATA-DICTIONARY.md 一致）：

* 支持域 = 采信控制点在像素域的凸包；
* 整幅余量 = 图框对角线长度的 ``FULL_MARGIN_RATIO``；
* 四角到凸包的距离都不超过整幅余量，才可整幅放行；
* 支持域面积不足图框面积的 ``MIN_AREA_RATIO`` 时整幅退回（点再多，挤在一角
  也不能签）；
* 其余可拟合、可接边但四角未全覆盖的图幅，只签支持域（限定范围）。
"""

from math import hypot

FULL_MARGIN_RATIO = 0.25
MIN_AREA_RATIO = 0.10
GEOMETRY_EPSILON = 1e-9

CORNERS = (
    ('NW', '西北', lambda width, height: (0.0, 0.0)),
    ('NE', '东北', lambda width, height: (width, 0.0)),
    ('SE', '东南', lambda width, height: (width, height)),
    ('SW', '西南', lambda width, height: (0.0, height)),
)


def _cross(origin, first, second):
    return ((first[0] - origin[0]) * (second[1] - origin[1])
            - (first[1] - origin[1]) * (second[0] - origin[0]))


def convex_hull(points):
    """单调链凸包，去掉重复点和落在边上的中间点。"""
    unique = sorted({(round(float(u), 9), round(float(v), 9)) for u, v in points})
    if len(unique) <= 2:
        return unique
    lower = []
    for point in unique:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], point) <= GEOMETRY_EPSILON:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(unique):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], point) <= GEOMETRY_EPSILON:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def polygon_area(polygon):
    if len(polygon) < 3:
        return 0.0
    total = 0.0
    for index, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(index + 1) % len(polygon)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2


def _inside(polygon, point):
    if len(polygon) < 3:
        return False
    signs = [_cross(polygon[index], polygon[(index + 1) % len(polygon)], point)
             for index in range(len(polygon))]
    return all(value >= -GEOMETRY_EPSILON for value in signs) or all(value <= GEOMETRY_EPSILON for value in signs)


def _segment_distance(point, first, second):
    dx, dy = second[0] - first[0], second[1] - first[1]
    if dx == 0 and dy == 0:
        return hypot(point[0] - first[0], point[1] - first[1])
    ratio = ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy) / (dx * dx + dy * dy)
    ratio = max(0.0, min(1.0, ratio))
    return hypot(point[0] - first[0] - ratio * dx, point[1] - first[1] - ratio * dy)


def hull_distance(polygon, point):
    """点到凸包的距离；包内为 0。线段退化为两点时取到最近端点。"""
    if not polygon:
        return float('inf')
    if len(polygon) == 1:
        return hypot(point[0] - polygon[0][0], point[1] - polygon[0][1])
    if len(polygon) == 2:
        return _segment_distance(point, polygon[0], polygon[1])
    if _inside(polygon, point):
        return 0.0
    return min(_segment_distance(point, polygon[index], polygon[(index + 1) % len(polygon)])
               for index in range(len(polygon)))


def _rounded(value):
    return round(float(value), 6)


def _basis_text(label, pixel, nearest_name, nearest_px, distance_px, margin_px, state):
    corner = f'{label}角 ({pixel[0]:.0f}, {pixel[1]:.0f})'
    nearest = f'最近采信点 {nearest_name} 距 {nearest_px:.1f} px' if nearest_name else '无采信控制点'
    if state == 'INSIDE':
        return f'{corner} 落在支持域内，{nearest}，可覆盖。'
    if state == 'WITHIN_MARGIN':
        return f'{corner} 在支持域外 {distance_px:.1f} px，未超整幅余量 {margin_px:.1f} px，{nearest}，可覆盖。'
    return f'{corner} 在支持域外 {distance_px:.1f} px，超过整幅余量 {margin_px:.1f} px，{nearest}，不支持整幅签审。'


def assess(sheet, accepted):
    """对一幅图做覆盖裁定；``accepted`` 为已采信控制点（含 pixel/name/id）。"""
    width, height = float(sheet['width_px']), float(sheet['height_px'])
    area = width * height
    diagonal = hypot(width, height)
    margin = FULL_MARGIN_RATIO * diagonal
    points = [(float(point['pixel'][0]), float(point['pixel'][1])) for point in accepted]
    hull = convex_hull(points)
    hull_area = polygon_area(hull)
    ratio = hull_area / area if area else 0.0
    corners = []
    for key, label, position in CORNERS:
        pixel = position(width, height)
        distance = hull_distance(hull, pixel) if hull else float('inf')
        nearest_px, nearest_name = None, None
        for point, row in zip(points, accepted):
            spread = hypot(point[0] - pixel[0], point[1] - pixel[1])
            if nearest_px is None or spread < nearest_px:
                nearest_px, nearest_name = spread, row['name']
        if not hull:
            state = 'UNSUPPORTED'
        elif distance <= GEOMETRY_EPSILON:
            state = 'INSIDE'
        elif distance <= margin + GEOMETRY_EPSILON:
            state = 'WITHIN_MARGIN'
        else:
            state = 'UNSUPPORTED'
        corners.append({
            'corner': key, 'label': f'{label}角', 'pixel': [pixel[0], pixel[1]],
            'nearest_control': nearest_name, 'nearest_px': None if nearest_px is None else _rounded(nearest_px),
            'hull_distance_px': None if distance == float('inf') else _rounded(distance),
            'supported': state != 'UNSUPPORTED', 'state': state,
            'basis': _basis_text(label, pixel, nearest_name, nearest_px or 0.0,
                                 distance if distance != float('inf') else 0.0, margin, state),
        })

    unsupported = [corner['corner'] for corner in corners if not corner['supported']]
    if len(accepted) < 3 or len(hull) < 3:
        verdict, reason = 'RETURN', 'COVERAGE_INSUFFICIENT'
        summary = '采信控制点不足三个或无法围成支持域，不能签发。'
    elif ratio + GEOMETRY_EPSILON < MIN_AREA_RATIO:
        verdict, reason = 'RETURN', 'COVERAGE_UNSUPPORTED'
        summary = (f'支持域仅占图框 {ratio * 100:.1f}%（低于 {MIN_AREA_RATIO * 100:.0f}%），'
                   f'控制点集中于局部，退回补测。')
    elif unsupported:
        verdict, reason = 'LIMITED', None
        summary = (f'支持域占图框 {ratio * 100:.1f}%，' + '、'.join(unsupported) +
                   ' 角超出整幅余量，仅可签支持域范围。')
    else:
        verdict, reason = 'FULL', None
        summary = f'四角均在整幅余量 {margin:.1f} px 内（支持域占图框 {ratio * 100:.1f}%），可整幅放行。'

    if verdict == 'FULL':
        scope = {'kind': 'FULL', 'label': '整幅', 'polygon': [[0.0, 0.0], [width, 0.0], [width, height], [0.0, height]],
                 'excluded_corners': []}
    elif verdict == 'LIMITED':
        scope = {'kind': 'LIMITED', 'label': '限定范围',
                 'polygon': [[x, y] for x, y in hull], 'excluded_corners': unsupported}
    else:
        scope = None
    return {
        'verdict': verdict, 'reason': reason, 'summary': summary,
        'method': 'accepted-control-hull',
        'control_count': len(accepted),
        'hull': [[x, y] for x, y in hull],
        'hull_area_px2': _rounded(hull_area), 'sheet_area_px2': _rounded(area),
        'area_ratio': _rounded(ratio),
        'full_margin_px': _rounded(margin), 'full_margin_ratio': FULL_MARGIN_RATIO,
        'min_area_ratio': MIN_AREA_RATIO,
        'corners': corners, 'unsupported_corners': unsupported,
        'scope': scope,
    }

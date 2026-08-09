"""Pure, versioned parametric assembly expansion for native Vectorworks boxes."""
import hashlib
import json
import math


COMMON = {
    'prefix': {'type': 'string', 'required': True},
    'layer': {'type': 'string', 'required': True},
    'class': {'type': 'string', 'default': 'None'},
    'x': {'type': 'number', 'default': 0.0},
    'y': {'type': 'number', 'default': 0.0},
    'z': {'type': 'number', 'default': 0.0},
    'material_name': {'type': 'string', 'default': ''},
    'texture_name': {'type': 'string', 'default': ''},
}


TEMPLATES = {
    'stud_wall@1': {
        'revision': 1,
        'description': 'Axis-aligned wood stud wall with bottom and single/double top plates.',
        'parameters': {
            **COMMON,
            'width': {'type': 'number', 'required': True, 'min': 3.0},
            'height': {'type': 'number', 'required': True, 'min': 3.0},
            'depth': {'type': 'number', 'default': 3.5, 'min': 0.1},
            'stud_width': {'type': 'number', 'default': 1.5, 'min': 0.1},
            'plate_height': {'type': 'number', 'default': 1.5, 'min': 0.1},
            'spacing': {'type': 'number', 'default': 16.0, 'min': 1.0},
            'double_top': {'type': 'boolean', 'default': True},
        },
    },
    'case_box@1': {
        'revision': 1,
        'description': 'Open-front cabinet carcass with sides, top, bottom, back, and shelves.',
        'parameters': {
            **COMMON,
            'width': {'type': 'number', 'required': True, 'min': 1.0},
            'depth': {'type': 'number', 'required': True, 'min': 1.0},
            'height': {'type': 'number', 'required': True, 'min': 1.0},
            'thickness': {'type': 'number', 'default': 0.75, 'min': 0.05},
            'back_thickness': {'type': 'number', 'default': 0.25, 'min': 0.01},
            'shelves': {'type': 'integer', 'default': 0, 'min': 0, 'max': 50},
        },
    },
    'platform_deck@1': {
        'revision': 1,
        'description': 'Rectangular platform with perimeter rims, spaced joists, and optional skin.',
        'parameters': {
            **COMMON,
            'width': {'type': 'number', 'required': True, 'min': 3.0},
            'depth': {'type': 'number', 'required': True, 'min': 3.0},
            'joist_height': {'type': 'number', 'default': 3.5, 'min': 0.1},
            'member_width': {'type': 'number', 'default': 1.5, 'min': 0.1},
            'spacing': {'type': 'number', 'default': 16.0, 'min': 1.0},
            'skin_thickness': {'type': 'number', 'default': 0.0, 'min': 0.0},
        },
    },
}


def catalog():
    return [{'id': key, 'description': value['description'],
             'parameters': value['parameters']} for key, value in sorted(TEMPLATES.items())]


def _normalize(template_id, raw):
    template = TEMPLATES.get(str(template_id or '').strip())
    if not template:
        return None, 'unknown template/version: %s' % template_id
    if not isinstance(raw, dict): return None, 'parameters must be an object'
    schema = template['parameters']
    unknown = sorted(set(raw) - set(schema))
    if unknown: return None, 'unknown parameters: %s' % ', '.join(unknown)
    out = {}
    for name, spec in schema.items():
        if name in raw: value = raw[name]
        elif 'default' in spec: value = spec['default']
        elif spec.get('required'): return None, 'missing required parameter: %s' % name
        else: continue
        kind = spec['type']
        try:
            if kind == 'number':
                if isinstance(value, bool): raise ValueError()
                value = float(value)
                if not math.isfinite(value): raise ValueError()
            elif kind == 'integer':
                if isinstance(value, bool) or int(value) != float(value): raise ValueError()
                value = int(value)
            elif kind == 'boolean':
                if not isinstance(value, bool): raise ValueError()
            elif kind == 'string':
                value = str(value).strip()
                if spec.get('required') and not value: raise ValueError()
        except Exception:
            return None, 'invalid %s parameter: %s' % (kind, name)
        if spec.get('min') is not None and value < spec['min']:
            return None, '%s is below minimum %s' % (name, spec['min'])
        if spec.get('max') is not None and value > spec['max']:
            return None, '%s exceeds maximum %s' % (name, spec['max'])
        out[name] = value
    return out, None


def _identity_params(params):
    movable = {'x', 'y', 'z', 'layer', 'class', 'material_name', 'texture_name', 'prefix'}
    return {key: params[key] for key in sorted(params) if key not in movable}


def _hash(template_id, params):
    encoded = json.dumps({'template': template_id,
                          'revision': TEMPLATES[template_id]['revision'],
                          'parameters': _identity_params(params)},
                         sort_keys=True, separators=(',', ':'))
    return hashlib.sha1(encoded.encode('utf-8')).hexdigest()[:8].upper()


def _item(params, template_hash, role, sequence, x, y, z, width, depth, height):
    row = {
        'name': '%s-%s-%02d-%s' % (params['prefix'], role, sequence, template_hash),
        'layer': params['layer'], 'class': params['class'],
        'x': round(x, 6), 'y': round(y, 6), 'z': round(z, 6),
        'width': round(width, 6), 'depth': round(depth, 6),
        'height': round(height, 6),
    }
    if params['material_name']: row['material_name'] = params['material_name']
    if params['texture_name']: row['texture_name'] = params['texture_name']
    return row


def _spaced_positions(span, member, spacing, max_positions):
    if span < 2 * member:
        raise ValueError('span must fit two non-overlapping end members')
    projected = 2 + int(math.floor((span - 2 * member) / spacing + 1e-9))
    if projected > max_positions:
        raise ValueError('template expansion exceeds max_items')
    end = span - member
    positions = [0.0]
    cursor = spacing
    while cursor <= end - member + 1e-9:
        positions.append(cursor); cursor += spacing
    if end - positions[-1] > 1e-9: positions.append(end)
    return positions


def _stud_wall(params, digest, max_items):
    x, y, z = params['x'], params['y'], params['z']
    width, height, depth = params['width'], params['height'], params['depth']
    sw, ph, spacing = params['stud_width'], params['plate_height'], params['spacing']
    top_count = 2 if params['double_top'] else 1
    clear = height - ph * (1 + top_count)
    if clear <= 0: raise ValueError('height is too small for requested plates')
    fixed_count = 1 + top_count
    positions = _spaced_positions(width, sw, spacing, max_items - fixed_count)
    rows = [_item(params, digest, 'PLATE-BOTTOM', 1, x, y, z, width, depth, ph)]
    for index in range(top_count):
        rows.append(_item(params, digest, 'PLATE-TOP', index + 1, x, y,
                          z + height - ph * (index + 1), width, depth, ph))
    for index, dx in enumerate(positions, 1):
        rows.append(_item(params, digest, 'STUD', index, x + dx, y, z + ph,
                          sw, depth, clear))
    return rows


def _case_box(params, digest, max_items):
    x, y, z = params['x'], params['y'], params['z']
    width, depth, height = params['width'], params['depth'], params['height']
    t, back = params['thickness'], params['back_thickness']
    inner_w, inner_h, usable_d = width - 2 * t, height - 2 * t, depth - back
    if min(inner_w, inner_h, usable_d) <= 0:
        raise ValueError('case dimensions are too small for material thicknesses')
    if params['shelves'] and inner_h / (params['shelves'] + 1) < t - 1e-9:
        raise ValueError('shelf pitch is smaller than material thickness')
    if 5 + params['shelves'] > max_items:
        raise ValueError('template expansion exceeds max_items')
    rows = [
        _item(params, digest, 'SIDE', 1, x, y, z, t, depth, height),
        _item(params, digest, 'SIDE', 2, x + width - t, y, z, t, depth, height),
        _item(params, digest, 'BOTTOM', 1, x + t, y, z, inner_w, usable_d, t),
        _item(params, digest, 'TOP', 1, x + t, y, z + height - t, inner_w, usable_d, t),
        _item(params, digest, 'BACK', 1, x + t, y + depth - back, z + t,
              inner_w, back, inner_h),
    ]
    for index in range(1, params['shelves'] + 1):
        shelf_z = z + t + inner_h * index / (params['shelves'] + 1) - t / 2.0
        rows.append(_item(params, digest, 'SHELF', index, x + t, y, shelf_z,
                          inner_w, usable_d, t))
    return rows


def _platform_deck(params, digest, max_items):
    x, y, z = params['x'], params['y'], params['z']
    width, depth = params['width'], params['depth']
    h, mw, spacing = params['joist_height'], params['member_width'], params['spacing']
    clear = depth - 2 * mw
    if clear <= 0 or width < 2 * mw:
        raise ValueError('platform dimensions are too small for member width')
    fixed_count = 2 + (1 if params['skin_thickness'] > 0 else 0)
    positions = _spaced_positions(width, mw, spacing, max_items - fixed_count)
    rows = [
        _item(params, digest, 'RIM', 1, x, y, z, width, mw, h),
        _item(params, digest, 'RIM', 2, x, y + depth - mw, z, width, mw, h),
    ]
    for index, dx in enumerate(positions, 1):
        rows.append(_item(params, digest, 'JOIST', index, x + dx, y + mw, z,
                          mw, clear, h))
    if params['skin_thickness'] > 0:
        rows.append(_item(params, digest, 'SKIN', 1, x, y, z + h,
                          width, depth, params['skin_thickness']))
    return rows


EXPANDERS = {'stud_wall@1': _stud_wall, 'case_box@1': _case_box,
             'platform_deck@1': _platform_deck}


def expand(template_id, raw, max_items=500):
    params, error = _normalize(template_id, raw)
    if error: return None, error
    try: max_items = int(max_items)
    except Exception: return None, 'max_items must be an integer'
    if not 1 <= max_items <= 500: return None, 'max_items must be 1..500'
    digest = _hash(template_id, params)
    try: items = EXPANDERS[template_id](params, digest, max_items)
    except ValueError as exc: return None, str(exc)
    if len(items) > max_items: return None, 'template expansion exceeds max_items'
    names = [item['name'] for item in items]
    if len(names) != len(set(names)): return None, 'template emitted duplicate names'
    items.sort(key=lambda row: row['name'])
    mins = {axis: min(item[axis] for item in items) for axis in ('x', 'y', 'z')}
    maxs = {
        'x': max(item['x'] + item['width'] for item in items),
        'y': max(item['y'] + item['depth'] for item in items),
        'z': max(item['z'] + item['height'] for item in items),
    }
    token_payload = {'template': template_id, 'parameters': params, 'items': items}
    token = hashlib.sha256(json.dumps(token_payload, sort_keys=True,
                                      separators=(',', ':')).encode('utf-8')).hexdigest()[:20]
    return {
        'template': template_id, 'template_hash': digest,
        'template_token': token, 'parameters': params, 'count': len(items),
        'bbox': {'min': mins, 'max': maxs}, 'items': items,
        'geometry': {'layer': params['layer'],
                     'name_prefix': '%s-' % params['prefix'], 'items': items,
                     'expected_count': len(items), 'prune': False},
        'instance_contract': ('prefix is the stable unique assembly-instance ID; '
                              'reuse it to relocate/update one instance, use a new '
                              'prefix for another placement'),
    }, None

"""
commands.py — vs.* implementations for VW MCP Bridge.

Command names + param schemas match vw_mcp_server.py (116 tools).
Runs on VW main thread (safe for all vs.* calls).

Key API facts:
  Points:   tuples  vs.Rect((x1,y1),(x2,y2))
  Colors:   0-65535 single tuple  vs.SetFillFore(h, (r,g,b))
  HRotate:  vs.HRotate(h, (cx,cy), angle_deg)
  HScale:   vs.HScale2D(h, cx, cy, sx, sy, scaleText)
  Layer vis:    vs.SetObjectVariableInt(layerH, 153, val)  -1=invis 0=normal 2=gray
  Layer type:   vs.GetObjectVariableInt(h, 154)  1=design 2=sheet
  Attach record: vs.SetRecord(h, recName)
  IFC: vs.IFC_GetIFCEntity(h)->(bool,str), vs.IFC_ExportNoUI(path)
  InternalIndex: vs.GetObjectVariableInt(h, 1165)
  FInLayer: vs.FInLayer(layerH)
  ForEachObject: build list in callback — never create/delete/re-layer inside
"""
import vs, traceback, os, json, time, uuid, math, hashlib
import assembly_templates as _assembly_templates

# ── vs.* signature index (knowledge index) ───────────────────────────────────
# vs_index.json (built by tools/build_vs_index.py from the SDK vs.py stub) maps
# every vs function -> {args, arity, required, ret, cat, doc}. Loaded once.
# `vsig(name)` gives an agent/self an instant accurate signature; `vcheck` lets
# call sites validate arity and fail with a clean dict instead of triggering a
# VW engine error dialog.
_VS_INDEX = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vs_index.json'),
              encoding='utf-8') as _f:
        _VS_INDEX = json.load(_f)
except Exception:
    _VS_INDEX = {}

# Guarded native-frame rebuild plans live in the Vectorworks process so the
# additive create and post-regeneration verify phases can be separate MCP calls.
# Preserve the store across importlib.reload() hot reloads; a VW restart expires
# plans cleanly instead of leaving hidden filesystem state behind.
_NATIVE_REBUILD_PLANS = globals().get('_NATIVE_REBUILD_PLANS', {})
_ASSEMBLY_NORMALIZE_PLANS = globals().get('_ASSEMBLY_NORMALIZE_PLANS', {})

def vsig(name):
    """Return the signature record for a vs.* function, or None."""
    return _VS_INDEX.get(name)

def vcheck(name, argc):
    """True if argc is a valid argument count for vs.<name> per the index.
    Unknown functions pass (index may lag the SDK)."""
    s = _VS_INDEX.get(name)
    if not s:
        return True
    return s['required'] <= argc <= s['arity']

# ── helpers ──────────────────────────────────────────────────────────────────

def _c8(v):
    return min(65535, int(v) * 257)

def _c255(v):
    return round(v / 257)

def _oid(h):
    """Return object UUID string. VW2026 uses UUIDs — InternalIndex APIs were removed."""
    if not h: return None
    try: return vs.GetObjectUuid(h) or None
    except Exception: return None

def _h(oid):
    """Resolve object_id (UUID string) → handle."""
    if oid is None: return None
    try:
        h = vs.GetObjectByUuid(str(oid))
        if h: return h
    except Exception: pass
    return None

def _safe(fn, default=None):
    try: return fn()
    except: return default

def _bbox(h):
    try:
        p1, p2 = vs.GetBBox(h)
        return {'x1': p1[0], 'y1': p1[1], 'x2': p2[0], 'y2': p2[1],
                'w': abs(p2[0]-p1[0]), 'h': abs(p2[1]-p1[1])}
    except: return None

OBJ_TYPES = {
    2:'line',3:'rect',4:'oval',5:'polyline',6:'bezier',8:'arc',
    9:'freehand',11:'group',12:'symbol_def',15:'symbol',21:'polygon',
    24:'extrude',25:'solid',26:'sweep',28:'sphere',34:'wall',40:'mesh',
    68:'plugin_obj_legacy',84:'solid_loft',86:'plugin_obj',89:'viewport',91:'nurbs',94:'worksheet'
}

def _summary(h):
    if not h: return None
    t = _safe(lambda: vs.GetTypeN(h), 0)
    return {
        'object_id': _oid(h),
        'type':      t,
        'type_name': OBJ_TYPES.get(t, f'type_{t}'),
        'name':      _safe(lambda: vs.GetName(h)),
        'class':     _safe(lambda: vs.GetClass(h)),
        'layer':     _safe(lambda: vs.GetLName(vs.GetLayer(h))),
        'bounds':    _bbox(h),
    }

def _collect(criteria, limit=500):
    handles = []
    def cb(h):
        if len(handles) < limit:
            handles.append(h)
    vs.ForEachObject(cb, criteria)
    return handles

def _active_class():
    # VW renamed across versions: try current → old
    for name in ('ActiveClass', 'GetActClassN', 'GetClass', 'GetClassN'):
        fn = getattr(vs, name, None)
        if fn:
            try: return fn()
            except: pass
    return ''

def _with_layer_class(params):
    """Activate layer+class if given; return prior (layer_name, class_name) to restore."""
    prev_layer = _safe(lambda: vs.GetLName(vs.ActLayer()))
    prev_class = _active_class()
    if params.get('layer'):
        vs.Layer(params['layer'])
    if params.get('class'):
        vs.NameClass(params['class'])
    return prev_layer, prev_class

def _restore(prev):
    try:
        if prev[0]: vs.Layer(prev[0])
        if prev[1]: vs.NameClass(prev[1])
    except: pass


# ── Document ────────────────────────────────────────────────────────────────

def ping(p):
    return {'status': 'ok', 'message': 'VW MCP Bridge running'}

def get_document_info(p):
    return {
        'name':       _safe(vs.GetFName),
        'path':       _safe(vs.GetFPathName),
        'vw_version': _safe(lambda: vs.GetVersion()),  # VW2026: GetVWVersion does not exist
    }

def workspace_snapshot(p):
    """Compact one-round-trip kickoff state for an agent session.

    Returns document, units, active layer, bounded selection, and optional
    assembly QA. Set include_layers=true only when a full layer inventory is
    needed; normal kickoff should stay compact.
    """
    selection = get_selected_objects({}) if bool(p.get('include_selection', True)) else None
    if selection:
        selection_limit = max(0, int(p.get('selection_limit', 20)))
        objects = selection.get('objects') or []
        selection = dict(selection)
        selection['objects'] = objects[:selection_limit]
        selection['objects_omitted'] = max(0, len(objects) - selection_limit)
    assembly = None
    if isinstance(p.get('assembly'), dict):
        assembly_params = dict(p['assembly'])
        assembly_params.setdefault('include_objects', False)
        assembly = assembly_qa_bundle(assembly_params)
    return {
        'status': 'ok',
        'document': get_document_info({}),
        'units': get_document_units({}),
        'active_layer': get_active_layer({}),
        'selection': selection,
        'layers': get_layers({}) if bool(p.get('include_layers', False)) else None,
        'assembly': assembly,
    }

def save_document(p):
    # VW2026: no SaveDocument. Drive the Save menu command (keeps path/format).
    _safe(lambda: vs.DoMenuTextByName('Save', 0))
    return {'status': 'ok'}

def save_document_as(p):
    path = p.get('path', '')
    if not path or not os.path.isabs(path):
        return {'error': 'absolute .vwx path required'}
    if not path.lower().endswith('.vwx'):
        return {'error': 'save path must end in .vwx'}
    # SaveActiveDocument(path) is NOT a safe headless Save As on macOS VW2026.
    # It returns before asynchronously opening a "Save changes before closing?"
    # modal, disables the menu-command pump, and makes every later MCP call time
    # out. A green return value is therefore actively misleading. Save the named
    # working file in place, then copy it at the filesystem layer instead.
    return {
        'error': ('save_document_as is disabled: Vectorworks opens a modal '
                  'dialog after the script returns. Save manually, or save the '
                  'named file in place and copy it outside Vectorworks.'),
        'code': 'MODAL_DIALOG_REQUIRED',
        'requested_path': path,
    }

def get_document_preferences(p):
    # vs.GetDocumentUnits does not exist on VW2026 — vs.GetUnits() does
    # (returns fractionalDisplay, displayAccuracy, format, unitsPerInch,
    # unitName, squareUnitName as a tuple in Python).
    out = {}
    u = _safe(lambda: vs.GetUnits())
    if u is not None:
        try:
            out['units'] = {'fraction': u[0], 'display': u[1], 'format': u[2],
                            'units_per_inch': u[3], 'name': u[4],
                            'square_name': u[5]}
        except Exception:
            out['units_raw'] = list(u) if isinstance(u, (list, tuple)) else str(u)
    out['active_layer_scale'] = _safe(lambda: vs.GetLScale(vs.ActLayer()))
    out['status'] = 'ok'
    return out

def set_document_preferences(p):
    # Use VW's standard unit presets rather than low-level SetUnits flags.
    # The MCP surface promises mm/cm/m/inch/feet; older bridge code silently
    # ignored the units argument, which left mechanical files in inches.
    unit_index = None
    requested_units = p.get('units')
    if requested_units:
        key = str(requested_units).strip().lower()
        unit_index = {
            'mm': 7, 'millimeter': 7, 'millimeters': 7,
            'cm': 8, 'centimeter': 8, 'centimeters': 8,
            'm': 9, 'meter': 9, 'meters': 9,
            'inch': 2, 'inches': 2, 'in': 2,
            'feet': 3, 'foot': 3, 'ft': 3,
        }.get(key)
        if unit_index is None:
            return {'error': 'unsupported units: %s (use mm/cm/m/inch/feet)'
                    % requested_units}
    if unit_index is not None:
        vs.Units(unit_index)
    # Drawing scale lives on the active design layer.
    if p.get('scale') is not None:
        h = vs.ActLayer()
        if h: vs.SetLScale(h, float(p['scale']))
    return {'status': 'ok', 'units': requested_units,
            'scale': p.get('scale')}


# ── Layers ──────────────────────────────────────────────────────────────────

def get_layers(p):
    layers = []
    h = vs.FLayer()
    while h:
        lt = _safe(lambda: vs.GetObjectVariableInt(h, 154), 1)
        layers.append({
            'object_id': _oid(h),
            'name':      _safe(lambda: vs.GetLName(h)),
            'type':      'sheet' if lt == 2 else 'design',
            'visible':   _safe(lambda: vs.GetObjectVariableInt(h, 153), 0) == 0,
            'scale':     _safe(lambda: vs.GetLScale(h)),
        })
        h = vs.NextLayer(h)
    return {'layers': layers, 'count': len(layers)}

def get_layer_info(p):
    name = p.get('name', '')
    h = vs.GetLayerByName(name)
    if not h: return {'error': f'Layer not found: {name}'}
    lt = _safe(lambda: vs.GetObjectVariableInt(h, 154), 1)
    return {
        'object_id': _oid(h),
        'name':      name,
        'type':      'sheet' if lt == 2 else 'design',
        'visible':   _safe(lambda: vs.GetObjectVariableInt(h, 153), 0) == 0,
        'scale':     _safe(lambda: vs.GetLScale(h)),
    }

def create_layer(p):
    name = p.get('name', 'New Layer')
    t = 2 if str(p.get('layer_type', 'design')).lower() == 'sheet' else 1
    h = vs.CreateLayer(name, t)
    if p.get('scale') is not None and h:
        _safe(lambda: vs.SetLScale(h, float(p['scale'])))
    return {'status': 'ok', 'name': name, 'object_id': _oid(h)}

def delete_layer(p):
    name = p.get('name', '')
    h = vs.GetLayerByName(name)
    if not h: return {'error': f'Layer not found: {name}'}
    active = _safe(lambda: vs.ActLayer())
    if active == h:
        return {'error': 'cannot delete the active layer; restore a Saved View on another '
                         'layer first, then retry'}
    vs.DelObject(h)
    if _safe(lambda: vs.GetLayerByName(name)):
        return {'error': 'Vectorworks did not delete layer: %s' % name}
    return {'status': 'ok', 'deleted': name}

def set_active_layer(p):
    # vs.Layer(name) PARKS the enclosing script frame in a nested message
    # loop on VW2026 (verified live 2026-07-06: crashed VW from a CEF
    # callback, hung it from a WM_TIMER, silently killed the file-pump from
    # a menu command). Never call vs.Layer from the bridge.
    name = p.get('name', '')
    h = vs.GetLayerByName(name)
    if not h:
        return {'error': f'Layer not found: {name} (set_active_layer no '
                         'longer auto-creates; use create_layer first)'}
    for fn in ('SetActiveLayerN', 'SetActiveLayer'):
        f = getattr(vs, fn, None)
        if f:
            _safe(lambda: f(h))
            now = _safe(lambda: vs.GetLName(vs.ActLayer()))
            if now == name:
                return {'status': 'ok', 'name': name, 'via': fn}
    return {'error': 'no safe layer-activation API on this VW build '
                     '(vs.Layer is quarantined — it parks the script '
                     'frame). Place objects via set_object_layer instead.'}

def get_active_layer(p):
    h = vs.ActLayer()
    return {'name': _safe(lambda: vs.GetLName(h)), 'object_id': _oid(h)}

def set_layer_visibility(p):
    name = p.get('name', '')
    visible = p.get('visible', True)
    h = vs.GetLayerByName(name)
    if not h: return {'error': f'Layer not found: {name}'}
    vs.SetObjectVariableInt(h, 153, 0 if visible else -1)
    return {'status': 'ok'}

def rename_layer(p):
    old = p.get('old_name', '')
    new = p.get('new_name', '')
    h = vs.GetLayerByName(old)
    if not h: return {'error': f'Layer not found: {old}'}
    vs.SetName(h, new)   # VW2026: no SetLName; SetName renames any named object
    return {'status': 'ok'}

def set_layer_scale(p):
    name = p.get('name', '')
    h = vs.GetLayerByName(name)
    if not h: return {'error': f'Layer not found: {name}'}
    vs.SetLScale(h, float(p.get('scale', 1.0)))
    return {'status': 'ok'}


# ── Classes ─────────────────────────────────────────────────────────────────

def get_classes(p):
    count = vs.ClassNum()
    classes = []
    for i in range(1, count + 1):
        # VW2026 exposes ClassList(index); GetClName does not exist and the
        # old fallback produced fake "Class_1" names that looked authoritative.
        n = _safe(lambda i=i: vs.ClassList(i), '')
        if not n:
            continue
        classes.append({
            'name': n, 'index': i,
            'visible': _safe(lambda: vs.GetCVis(n) == 0),
        })
    return {'classes': classes, 'count': len(classes)}

def _class_names_all():
    """Return every document class, including classes with no objects yet."""
    out = []
    for i in range(1, (vs.ClassNum() or 0) + 1):
        name = _safe(lambda i=i: vs.ClassList(i), '')
        if name:
            out.append(name)
    return out

def get_class_styles(p):
    """Per-class appearance for QGIS/GIS styling (VW2026-safe).
    Pass {'names': [...]} to narrow the readback. Colors returned as 0-255 RGB."""
    names = p.get('names') or _class_names_all()
    out = {}
    for nm in names:
        d = {}
        ff = _safe(lambda: vs.GetClFillFore(nm))
        d['fill'] = [_c255(ff[0]), _c255(ff[1]), _c255(ff[2])] if ff else None
        pf = _safe(lambda: vs.GetClPenFore(nm))
        d['pen'] = [_c255(pf[0]), _c255(pf[1]), _c255(pf[2])] if pf else None
        d['lineweight'] = _safe(lambda: vs.GetClLW(nm))      # VW mils; mm = lw * 0.0254
        d['fill_pattern'] = _safe(lambda: vs.GetClFPat(nm))  # 0=none, 1/2=solid, 14=hatch, neg=tile
        d['visible'] = _safe(lambda: vs.GetCVis(nm) == 0)
        out[nm] = d
    return {'count': len(out), 'classes': out}

def create_class(p):
    name = p.get('name', '')
    vs.NameClass(name)
    return {'status': 'ok', 'class': name}

def delete_class(p):
    name = p.get('name', '')
    try:
        vs.DelClass(name)
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}

def set_active_class(p):
    vs.NameClass(p.get('name', 'None'))
    return {'status': 'ok'}

def set_class_visibility(p):
    # VW2026: no SetClassVisibility. Use ShowClass / HideClass by name.
    name = p.get('name', '')
    if p.get('visible', True):
        vs.ShowClass(name)
    else:
        vs.HideClass(name)
    return {'status': 'ok'}

def rename_class(p):
    old = p.get('old_name', '')
    new = p.get('new_name', '')
    try:
        vs.RenameClass(old, new)
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}

def set_class_appearance(p):
    name = p.get('name', '')
    try:
        if any(k in p for k in ('fill_r', 'fill_g', 'fill_b')):
            r = _c8(p.get('fill_r', 255))
            g = _c8(p.get('fill_g', 255))
            b = _c8(p.get('fill_b', 255))
            vs.SetClFillBack(name, (r, g, b))
            vs.SetClFillFore(name, (r, g, b))
        if any(k in p for k in ('pen_r', 'pen_g', 'pen_b')):
            r = _c8(p.get('pen_r', 0))
            g = _c8(p.get('pen_g', 0))
            b = _c8(p.get('pen_b', 0))
            vs.SetClPenBack(name, (r, g, b))
            vs.SetClPenFore(name, (r, g, b))
        if 'line_weight' in p and p['line_weight'] is not None:
            # VW lineweight is mil = mm * ~3.9 (1 mil = 1/1000 inch). VW API uses mils.
            mm = float(p['line_weight'])
            vs.SetClLW(name, int(mm * 100))   # LW units in VW are 0.01mm mils-ish
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}


# ── Object Query ────────────────────────────────────────────────────────────

def get_objects(p):
    parts = []
    if p.get('criteria'): parts.append(p['criteria'])
    if p.get('layer'):    parts.append(f"L='{p['layer']}'")
    if p.get('class'):    parts.append(f"C='{p['class']}'")
    if p.get('type'):     parts.append(f"T={p['type'].upper()}")
    crit = ' & '.join(parts) if parts else 'ALL'
    hs = _collect(crit, p.get('limit', 100))
    return {'objects': [_summary(h) for h in hs], 'count': len(hs)}

def get_object_info(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    info = _summary(h)
    rf = _safe(lambda: vs.GetFillFore(h))
    rp = _safe(lambda: vs.GetPenFore(h))
    info['fill_color'] = [_c255(v) for v in rf] if rf else None
    info['pen_color']  = [_c255(v) for v in rp] if rp else None
    info['lineweight'] = _safe(lambda: vs.GetLW(h))
    info['opacity']    = _safe(lambda: vs.GetOpacity(h))
    return info

def get_selected_objects(p):
    hs = _collect('SEL=TRUE', 500)
    return {'objects': [_summary(h) for h in hs], 'count': len(hs)}

def select_objects(p):
    vs.DSelectAll()
    for oid in p.get('object_ids', []):
        h = _h(oid)
        if h: vs.SetSelect(h)
    return {'status': 'ok'}

def deselect_all(p):
    vs.DSelectAll()
    return {'status': 'ok'}

def get_object_bounds(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    return _bbox(h) or {'error': 'No bounds'}

def count_objects(p):
    parts = []
    if p.get('layer'): parts.append(f"L='{p['layer']}'")
    if p.get('class'): parts.append(f"C='{p['class']}'")
    if p.get('type'):  parts.append(f"T={p['type'].upper()}")
    crit = ' & '.join(parts) if parts else 'ALL'
    return {'count': len(_collect(crit))}

def find_objects_by_name(p):
    name = p.get('name', '')
    hs = _collect(f"N='{name}'")
    return {'objects': [_summary(h) for h in hs], 'count': len(hs)}


# ── Object Manipulation (singular, per server spec) ─────────────────────────

def move_object(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    vs.HMove(h, p.get('dx', 0.0), p.get('dy', 0.0))
    return {'status': 'ok'}

def rotate_object(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    bb = _bbox(h)
    cx = p.get('cx', (bb['x1']+bb['x2'])/2 if bb else 0)
    cy = p.get('cy', (bb['y1']+bb['y2'])/2 if bb else 0)
    vs.HRotate(h, (cx, cy), p.get('angle', 0.0))
    return {'status': 'ok'}

def scale_object(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    bb = _bbox(h)
    cx = p.get('cx', (bb['x1']+bb['x2'])/2 if bb else 0)
    cy = p.get('cy', (bb['y1']+bb['y2'])/2 if bb else 0)
    vs.HScale2D(h, cx, cy, p.get('sx', 1.0), p.get('sy', 1.0), False)
    return {'status': 'ok'}

def delete_object(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    vs.DelObject(h)
    return {'status': 'ok'}

def duplicate_object(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    nh = vs.HDuplicate(h, p.get('dx', 0.0), p.get('dy', 0.0))
    return {'status': 'ok', 'object_id': _oid(nh)}

def set_object_layer(p):
    h = _h(p.get('object_id'))
    layer = p.get('layer', '')
    if not h: return {'error': 'Object not found'}
    try:
        lh = vs.GetLayerByName(layer)
        if not lh: return {'error': f'Layer not found: {layer}'}
        vs.SetParent(h, lh)
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}

def set_object_class(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    vs.SetClass(h, p.get('class', ''))
    return {'status': 'ok'}

def set_object_name(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    vs.SetName(h, p.get('name', ''))
    return {'status': 'ok'}

def group_objects(p):
    vs.DSelectAll()
    for oid in p.get('object_ids', []):
        h = _h(oid)
        if h: vs.SetSelect(h)
    vs.DoMenuTextByName('Group', 0)
    # LNewObj does not see menu-created groups; the new group stays selected,
    # so the first selected object on the active layer IS the group.
    gh = vs.FSActLayer()
    return {'status': 'ok', 'object_id': _oid(gh)}

def ungroup_object(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    vs.DSelectAll(); vs.SetSelect(h)
    vs.DoMenuTextByName('Ungroup', 0)
    return {'status': 'ok'}

def mirror_object(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    axis = p.get('axis', 'vertical')
    bb = _bbox(h)
    cx = p.get('x', (bb['x1']+bb['x2'])/2 if bb else 0)
    cy = p.get('y', (bb['y1']+bb['y2'])/2 if bb else 0)
    dup = bool(p.get('duplicate', False))
    # VW2026: no HMirror. MirrorN(h, dup, p1, p2, preserveMatrix) — axis
    # through two points; preserveMatrix=True keeps symbol/PIO matrices sane.
    if axis == 'horizontal':
        r = vs.MirrorN(h, dup, (cx-10, cy), (cx+10, cy), True)
    else:
        r = vs.MirrorN(h, dup, (cx, cy-10), (cx, cy+10), True)
    out = {'status': 'ok'}
    if dup and r:
        out['object_id'] = _oid(r)
    return out
def _newobj_result(p, fallback=None):
    """Resolve the 'just created' handle. Prefer an explicit fallback because
    some creators (notably CreateCustomObjectPath + DTM6_SendToSurface) do NOT
    advance vs.LNewObj() — the caller must pass the handle it captured."""
    h = fallback if fallback else vs.LNewObj()
    if h and p.get('class'):
        try: vs.SetClass(h, p['class'])
        except Exception: pass
    return {'status': 'ok', 'object_id': _oid(h)}

def draw_line(p):
    prev = _with_layer_class(p)
    try:
        vs.MoveTo((p.get('x1',0), p.get('y1',0)))
        vs.LineTo((p.get('x2',100), p.get('y2',0)))
        return _newobj_result(p)
    finally:
        _restore(prev)

def draw_rectangle(p):
    prev = _with_layer_class(p)
    try:
        vs.Rect((p.get('x1',0), p.get('y1',0)),
                (p.get('x2',100), p.get('y2',100)))
        return _newobj_result(p)
    finally:
        _restore(prev)

def draw_circle(p):
    # VW2026: ArcByCenter((cx,cy), r, 0, 360) returns null UUID — use Oval bbox instead.
    prev = _with_layer_class(p)
    try:
        cx, cy, r = p.get('cx', 0), p.get('cy', 0), p.get('radius', 50)
        vs.Oval(cx - r, cy + r, cx + r, cy - r)   # left, top, right, bottom
        h = vs.LNewObj()
        return _newobj_result(p, fallback=h)
    finally:
        _restore(prev)

def draw_arc(p):
    # VW2026: ArcByCenter returns null UUID for partial arcs too — use vs.Arc bbox form.
    # vs.Arc(left, top, right, bottom, start_angle, sweep_angle). VERIFIED live 2026-06-25:
    # the 6th arg is the SWEEP (included) angle, not the end angle — GetArc readback of
    # Arc(...,30,90) returns (30, 90). Pass sweep directly, NOT start+sweep.
    prev = _with_layer_class(p)
    try:
        cx, cy = p.get('cx', 0), p.get('cy', 0)
        r = p.get('radius', 50)
        start = p.get('start_angle', 0)
        sweep = p.get('sweep_angle', 90)
        vs.Arc(cx - r, cy + r, cx + r, cy - r, start, sweep)
        h = vs.LNewObj()
        return _newobj_result(p, fallback=h)
    finally:
        _restore(prev)

def draw_ellipse(p):
    prev = _with_layer_class(p)
    try:
        cx, cy = p.get('cx', 0), p.get('cy', 0)
        rx, ry = p.get('rx', 50), p.get('ry', 25)
        vs.Oval((cx-rx, cy-ry), (cx+rx, cy+ry))
        return _newobj_result(p)
    finally:
        _restore(prev)

def draw_polyline(p):
    prev = _with_layer_class(p)
    try:
        pts = p.get('points', [])
        if not pts: return {'error': 'No points'}
        closed = p.get('closed', False)
        if not closed:
            vs.OpenPoly()
        else:
            vs.ClosePoly()
        vs.BeginPoly()
        for pt in pts:
            vs.Add2DVertex((pt[0], pt[1]), 0, 0)
        vs.EndPoly()
        return _newobj_result(p)
    finally:
        _restore(prev)

def draw_text(p):
    prev = _with_layer_class(p)
    try:
        x, y = p.get('x', 0), p.get('y', 0)
        vs.TextOrigin((x, y))
        amap = {'left': 1, 'center': 2, 'right': 3}
        vs.TextJust(amap.get(p.get('align','left'), 1))
        vs.TextSize(p.get('font_size', 12))
        vs.CreateText(p.get('text', ''))
        return _newobj_result(p)
    finally:
        _restore(prev)

def draw_dimension(p):
    prev = _with_layer_class(p)
    try:
        # VW2026: no LinDimN. LinearDim(start, end, offset, dimType, arrow, textFlag, textOffset)
        vs.LinearDim((p.get('x1',0), p.get('y1',0)),
                     (p.get('x2',100), p.get('y2',0)),
                     p.get('offset', 20), 1, 0, 0, 0.0)
        return _newobj_result(p)
    finally:
        _restore(prev)

def draw_spline(p):
    prev = _with_layer_class(p)
    try:
        pts = p.get('points', [])
        if not pts: return {'error': 'No points'}
        vs.OpenPoly()
        vs.BeginPoly()
        for pt in pts:
            vs.Add2DVertex((pt[0], pt[1]), 2, 0)   # vtxType 2 = cubic
        vs.EndPoly()
        return _newobj_result(p)
    finally:
        _restore(prev)


# ── 3D Drawing ──────────────────────────────────────────────────────────────

def draw_extrude(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': '2D object not found'}
    eh = vs.HExtrude(h, 0, p.get('height', 100))   # VW2026: CreateExtrude does not exist
    return {'status': 'ok', 'object_id': _oid(eh)}

def draw_box(p):
    prev = _with_layer_class(p)
    try:
        x, y, z = p.get('x', 0), p.get('y', 0), p.get('z', 0)
        w, d, ht = p.get('width', 100), p.get('depth', 100), p.get('height', 100)
        vs.Rect((x, y), (x+w, y+d))
        rh = vs.LNewObj()
        bh = vs.HExtrude(rh, z, z + ht)   # VW2026: CreateExtrude does not exist; z baked in
        return {'status': 'ok', 'object_id': _oid(bh)}
    finally:
        _restore(prev)

def draw_sphere(p):
    prev = _with_layer_class(p)
    try:
        h = vs.CreateSphere(
            (p.get('cx',0), p.get('cy',0), p.get('cz',0)),
            p.get('radius', 50)
        )
        return {'status': 'ok', 'object_id': _oid(h)}
    finally:
        _restore(prev)

def draw_cone(p):
    prev = _with_layer_class(p)
    try:
        cx, cy, cz = p.get('cx',0), p.get('cy',0), p.get('cz',0)
        r = p.get('radius', 50); ht = p.get('height', 100)
        # Create via rotate of triangle — simpler: use CreateCone if exists
        try:
            h = vs.CreateCone((cx, cy, cz), (0, 0, 1), r, 0, ht)
        except Exception:
            # Fallback: polygon + sweep
            vs.Poly((cx, cy), (cx+r, cy), (cx, cy+ht))
            rh = vs.LNewObj()
            h = vs.Sweep(rh, 0, 360, 16, False)
        return {'status': 'ok', 'object_id': _oid(h)}
    finally:
        _restore(prev)

def draw_cylinder(p):
    prev = _with_layer_class(p)
    try:
        cx, cy, cz = p.get('cx',0), p.get('cy',0), p.get('cz',0)
        r = p.get('radius', 50); ht = p.get('height', 100)
        # VW2026: ArcByCenter returns null UUID — use Oval bbox instead (same fix as draw_circle)
        vs.Oval(cx - r, cy + r, cx + r, cy - r)   # left, top, right, bottom
        circle = vs.LNewObj()
        eh = vs.HExtrude(circle, cz, cz + ht)   # VW2026: CreateExtrude does not exist; cz baked in
        return {'status': 'ok', 'object_id': _oid(eh)}
    finally:
        _restore(prev)

# ── SDK enrichment: 3D modeling ─────────────────────────────────────────────

def create_extrude_along_path(p):
    """Sweep a 2D profile along a 2D/3D path. params: {path_id, profile_id}."""
    path = _h(p.get('path_id'))
    prof = _h(p.get('profile_id'))
    if not path or not prof:
        return {'error': 'path_id and profile_id (both existing objects) required'}
    eh = vs.ExtrudeAlongPath(path, prof)
    if not eh:
        return {'error': 'ExtrudeAlongPath produced no solid — profile and path '
                'are likely COPLANAR (degenerate sweep). The profile plane must '
                'be ~perpendicular to the path start; use a 3D path that rises in '
                'Z, or a profile on a different plane.'}
    return {'status': 'ok', 'object_id': _oid(eh)}

def create_tapered_extrude(p):
    """Extrude a 2D profile with a draft angle. params: {object_id, angle, height}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': '2D profile not found'}
    eh = vs.CreateTaperedExtrude(h, float(p.get('angle', 10)), float(p.get('height', 100)))
    return {'status': 'ok', 'object_id': _oid(eh)}

def create_loft(p):
    """Loft/skin NURBS surfaces through a GROUP of cross-section curves.
    params: {group_id, ruled(bool), closed(bool), solid(bool)}."""
    g = _h(p.get('group_id'))
    if not g: return {'error': 'group_id (a group of section curves) required'}
    h = vs.CreateLoftSurfaces(g, bool(p.get('ruled', False)),
                              bool(p.get('closed', False)), bool(p.get('solid', False)))
    return {'status': 'ok', 'object_id': _oid(h)}

def draw_locus(p):
    """2D reference point (locus). params: {x, y}."""
    prev = _with_layer_class(p)
    try:
        vs.Locus((float(p.get('x', 0)), float(p.get('y', 0))))
        return _newobj_result(p)
    finally:
        _restore(prev)

def draw_locus_3d(p):
    """3D reference point. params: {x, y, z}."""
    prev = _with_layer_class(p)
    try:
        vs.Locus3D((float(p.get('x', 0)), float(p.get('y', 0)), float(p.get('z', 0))))
        return _newobj_result(p)
    finally:
        _restore(prev)

def rotate_object_3d(p):
    """Rotate a 3D object about a point. params: {object_id, x_angle, y_angle,
    z_angle, cx, cy, cz}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    vs.Set3DRot(h, float(p.get('x_angle', 0)), float(p.get('y_angle', 0)),
                float(p.get('z_angle', 0)), float(p.get('cx', 0)),
                float(p.get('cy', 0)), float(p.get('cz', 0)))
    return {'status': 'ok', 'object_id': p.get('object_id')}

def get_3d_info(p):
    """Height/width/depth of a 3D object bounding box. params: {object_id}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    r = _safe(lambda: vs.Get3DInfo(h))
    if not r: return {'error': 'not a 3D object / no info'}
    try:
        return {'status': 'ok', 'height': r[0], 'width': r[1], 'depth': r[2]}
    except Exception:
        return {'status': 'ok', 'info': r}

def get_centroid_3d(p):
    """Center of gravity of a 3D object. params: {object_id}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    r = _safe(lambda: vs.Centroid3D(h))
    if r is None: return {'error': 'no centroid (not a solid?)'}
    # VW2026 Python: Centroid3D returns a FLATTENED (ok, x, y, z) 4-tuple, or a
    # nested (ok, point) — handle both.
    if isinstance(r, (list, tuple)):
        if len(r) >= 4 and all(isinstance(v, (int, float)) for v in r[1:4]):
            return {'status': 'ok', 'x': r[1], 'y': r[2], 'z': r[3]}
        if len(r) >= 2 and isinstance(r[1], (list, tuple)) and len(r[1]) >= 3:
            return {'status': 'ok', 'x': r[1][0], 'y': r[1][1], 'z': r[1][2]}
    return {'status': 'ok', 'centroid': r}

# ── SDK enrichment: 2D surface booleans (paint-bucket style) ────────────────

def add_surface(p):
    """Union two 2D surfaces into one. params: {object_id_a, object_id_b}."""
    a = _h(p.get('object_id_a')); b = _h(p.get('object_id_b'))
    if not a or not b: return {'error': 'object_id_a and object_id_b required'}
    h = vs.AddSurface(a, b)
    return {'status': 'ok', 'object_id': _oid(h)}

def clip_surface(p):
    """Subtract surface B from surface A. params: {object_id_a, object_id_b}."""
    a = _h(p.get('object_id_a')); b = _h(p.get('object_id_b'))
    if not a or not b: return {'error': 'object_id_a and object_id_b required'}
    vs.ClipSurface(a, b)
    return {'status': 'ok', 'object_id': _oid(vs.LNewObj())}

def intersect_surface(p):
    """Keep only the overlap of two 2D surfaces. params: {object_id_a, object_id_b}."""
    a = _h(p.get('object_id_a')); b = _h(p.get('object_id_b'))
    if not a or not b: return {'error': 'object_id_a and object_id_b required'}
    h = vs.IntersectSurface(a, b)
    return {'status': 'ok', 'object_id': _oid(h)}

def combine_into_surface(p):
    """DISABLED. vs.CombineIntoSurface (paint-bucket) WEDGES VW's main thread when
    the point is not inside a cleanly bounded, fillable region — the call never
    returns, freezing VW (verified). No safe headless precondition exists, so this
    verb refuses by default. Pass force=True to run it anyway at your own risk."""
    if not p.get('force'):
        return {'error': 'combine_into_surface is disabled: vs.CombineIntoSurface '
                'can hang VW main thread on an unbounded/unfillable region. '
                'Draw a CLOSED filled polygon instead, or pass force=True to override.'}
    h = vs.CombineIntoSurface((float(p.get('x', 0)), float(p.get('y', 0))))
    return {'status': 'ok', 'object_id': _oid(h)}

def add_hole(p):
    """Cut a hole in object A using object B as the hole template (B consumed).
    params: {object_id, hole_id}."""
    a = _h(p.get('object_id')); b = _h(p.get('hole_id'))
    if not a or not b: return {'error': 'object_id and hole_id required'}
    vs.AddHole(a, b)
    return {'status': 'ok', 'object_id': p.get('object_id')}

# ── SDK enrichment: graphic calculation (pure math, no side effects) ─────────

def _pts_from(raw):
    """Pull coordinate points out of a VS calc return (mix of bools + point
    lists). Returns list of [x,y(,z)] found, in order."""
    out = []
    if isinstance(raw, (list, tuple)):
        for e in raw:
            if isinstance(e, (list, tuple)) and e and all(isinstance(x,(int,float)) for x in e):
                out.append([float(x) for x in e])
    return out

def line_line_intersection(p):
    """Intersection point of two lines (each by 2 points).
    params: {a1:[x,y], a2:[x,y], b1:[x,y], b2:[x,y]}."""
    r = vs.LineLineIntersection(tuple(p['a1']), tuple(p['a2']),
                                tuple(p['b1']), tuple(p['b2']))
    pts = _pts_from(r)
    return {'status': 'ok', 'point': pts[0] if pts else None,
            'parallel': not pts, 'raw': r}

def circle_circle_intersection(p):
    """Intersection of two circles. params: {c1:[x,y], r1, c2:[x,y], r2}."""
    r = vs.CircleCircleInters(tuple(p['c1']), tuple(p['c2']),
                              float(p['r1']), float(p['r2']))
    pts = _pts_from(r)
    return {'status': 'ok', 'points': pts, 'count': len(pts), 'raw': r}

def line_circle_intersection(p):
    """Intersection of a line (2 pts) and a circle. params: {p1, p2, center, radius}."""
    r = vs.LineCircleIntersect(tuple(p['p1']), tuple(p['p2']),
                               tuple(p['center']), float(p['radius']))
    pts = _pts_from(r)
    return {'status': 'ok', 'points': pts, 'count': len(pts), 'raw': r}

def three_point_center(p):
    """Center of the circle through 3 points. params: {p1, p2, p3}."""
    r = vs.ThreePtCenter(tuple(p['p1']), tuple(p['p2']), tuple(p['p3']))
    try:
        return {'status': 'ok', 'x': r[0], 'y': r[1]}
    except Exception:
        return {'status': 'ok', 'center': r}

def polygon_area_at_point(p):
    """Area of the smallest bounded polygon surrounding a point (paint-bucket
    measure). params: {x, y}."""
    a = vs.SrndArea((float(p.get('x', 0)), float(p.get('y', 0))))
    return {'status': 'ok', 'area': a}

def polygonize(p):
    """Convert a polyline/polygon's arcs into straight segments.
    params: {object_id, segment_length, straight(bool)}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    r = vs.Polygonize(h, float(p.get('segment_length', 10)), bool(p.get('straight', False)))
    return {'status': 'ok', 'object_id': _oid(r)}

# ── ported from fix/material-classify (lost in the v7+ rewrites) ──────────

# containers whose children hold their own attributes/materials — descend into these.
_CONTAINER_TYPES = (11, 15, 86)  # group, symbol(instance), plug-in object

def _vw1(v):
    """Several VW2026 getters return a (success_flag, value[, extra]) tuple
    instead of a bare value (e.g. GetObjMaterialName, GetNumberOfComponents,
    GetComponentWidth/Class/Function, IFC_GetIFCEntity, IFC_GetEntityProp).
    Normalise: return the value element, or the raw value if not a flag-tuple."""
    if isinstance(v, (tuple, list)):
        if len(v) >= 2 and isinstance(v[0], bool):
            return v[1]
        return v[0] if len(v) == 1 else v
    return v


def _material_name(mh):
    return _safe(lambda: vs.GetName(mh)) if mh else None


def _design_layers(names=None):
    """Yield (name, layerHandle) for design layers, optionally filtered to `names`."""
    L = _safe(lambda: vs.FLayer())
    want = set(names) if names else None
    out = []
    while L:
        nm = _safe(lambda: vs.GetLName(L))
        is_design = _safe(lambda: vs.GetObjectVariableInt(L, 154) == 1)  # 154: layer type (1=design)
        if (want is None or nm in want) and (is_design or want is not None):
            out.append((nm, L))
        L = _safe(lambda: vs.NextLayer(L))
    return out


def _walk_deep(start_h, visit, guard=60000):
    """Handle-based recursive walk that DESCENDS into groups/symbols/PIOs — the
    thing vs.ForEachObject (top-level only) and vs.ForEachMaterial (broken callback
    marshalling in the embedded interpreter) can't do on VW2026. `visit(h)` is
    called for every object reached. Returns count of objects visited.
    Bounded by `guard` to stay under the bridge's per-tool timeout."""
    seen = [0]
    def rec(h):
        while h and seen[0] < guard:
            seen[0] += 1
            try: visit(h)
            except Exception: pass
            t = _safe(lambda: vs.GetTypeN(h), 0)
            if t in _CONTAINER_TYPES:
                sub = _safe(lambda: vs.FInGroup(h))
                if sub: rec(sub)
            h = _safe(lambda: vs.NextObj(h))
    rec(start_h)
    return seen[0]


def get_materials(p):
    """Enumerate materials actually used in the document by deep-walking objects
    (descends into groups/symbols/PIOs, where landscape material buildups live).

    Params (all optional):
      layers: list of design-layer names to restrict to (default: all design layers)
      guard:  max objects to visit per layer (default 60000)
    Returns distinct material names with usage counts.

    Why not a resource list: VW2026 has no BuildResourceList type for materials and
    vs.ForEachMaterial's callback is broken in the embedded interpreter, so this
    walks the geometry instead. GetName() works on material handles; object-level
    (GetObjMaterialHandle) and component-level (GetComponentMaterial) are both read."""
    names = p.get('layers')
    if isinstance(names, str): names = [names]
    guard = int(p.get('guard', 60000))
    counts = {}
    def visit(h):
        mh = _safe(lambda: vs.GetObjMaterialHandle(h))
        nm = _material_name(mh)
        if nm: counts[nm] = counts.get(nm, 0) + 1
        n = _vw1(_safe(lambda: vs.GetNumberOfComponents(h)))
        n = int(n or 0)
        for i in range(1, n + 1):
            cm = _safe(lambda i=i: vs.GetComponentMaterial(h, i))
            cn = _material_name(cm)
            if cn: counts[cn] = counts.get(cn, 0) + 1
    layers = _design_layers(names)
    visited = 0
    for _, lh in layers:
        visited += _walk_deep(_safe(lambda lh=lh: vs.FInLayer(lh)), visit, guard)
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    return {
        'count': len(counts),
        'objects_visited': visited,
        'layers_scanned': [nm for nm, _ in layers],
        'materials': [{'name': k, 'uses': v} for k, v in ordered],
    }


# ── assembly adaptation / completeness helpers ─────────────────────────────

def _float_or(v, default=0.0):
    try: return float(v)
    except Exception: return default


def _center3(h):
    """Safe world-axis bounding center for a prefiltered leaf object.

    Get3DCntr is cheap on PIOs, symbols, extrudes, and mesh leaves. Deliberately
    do not fall back to Centroid3D: broad Centroid3D scans on imported groups can
    peg VW at 100% CPU.
    """
    c = _safe(lambda: vs.Get3DCntr(h))
    if not isinstance(c, (tuple, list)):
        return None
    try:
        if len(c) >= 2 and isinstance(c[0], (tuple, list)) and len(c[0]) >= 2:
            return {'x': float(c[0][0]), 'y': float(c[0][1]), 'z': float(c[1])}
        if len(c) >= 3:
            return {'x': float(c[0]), 'y': float(c[1]), 'z': float(c[2])}
    except Exception:
        return None
    return None


def _dims3(h):
    """Return observed world-axis extents.

    VW2026 Get3DInfo reports (Y extent, X extent, Z extent) for the axis-aligned
    cart members used by this workflow. Keep both the named dict and raw order so
    callers can audit that assumption on unusual/rotated geometry.
    """
    d = _safe(lambda: vs.Get3DInfo(h))
    if not isinstance(d, (tuple, list)) or len(d) < 3:
        return None, None
    try:
        raw = [abs(float(d[0])), abs(float(d[1])), abs(float(d[2]))]
    except Exception:
        return None, None
    return {'x': raw[1], 'y': raw[0], 'z': raw[2]}, raw


def _primary_axis(dims):
    if not dims: return None
    return max(('x', 'y', 'z'), key=lambda k: dims.get(k, 0.0))


def _pio_record(h):
    rh = _safe(lambda: vs.GetParametricRecord(h))
    return (_safe(lambda: vs.GetName(rh)) or '') if rh else ''


def _assembly_role(profile, symbol_name, obj_type, dims, units_per_inch=1.0):
    text = ('%s %s' % (profile or '', symbol_name or '')).lower()
    if 'strap' in text: return 'strap'
    if 'aluminum l' in text or profile.upper().startswith('L_'): return 'angle'
    if 'tube' in text or 'square tube' in text: return 'tube'
    if 'wheel' in text or 'caster' in text: return 'wheel'
    if 'pr, prt' in text: return 'caster'
    if 'assembly~2_defintion' in text: return 'figure'
    if 'crate' in text or 'group#153' in text: return 'crate'
    # Lofted upholstery and other type-84 solids are terminal custom parts,
    # not lumber; their proportions can otherwise misclassify them as tubes.
    if obj_type == 84: return 'part'
    if dims:
        ordered = sorted(dims.values())
        thin = 0.2 * units_per_inch
        minimum_length = 4.0 * units_per_inch
        maximum_section = 4.0 * units_per_inch
        if ordered[0] <= thin and ordered[2] >= minimum_length:
            return 'strap' if ordered[1] <= 1.25 * units_per_inch else 'panel'
        if (ordered[1] <= maximum_section and ordered[2] >= minimum_length and
                ordered[2] / max(ordered[1], 1e-9) >= 2.5):
            return 'tube'
    if obj_type == 15: return 'symbol'
    if profile: return 'structural_member'
    return 'part'


def _is_assembly_leaf_part(profile, symbol_name, obj_type):
    return bool(profile or symbol_name or obj_type in (24, 40, 68, 84, 86))


def get_assembly_manifest(p):
    """Safely inventory an assembly on one design layer.

    Params:
      layer (required): design-layer name
      region: optional {x1,y1,x2,y2} world-center filter
      roles/profiles: optional allowlists
      name_prefix: optional object-name prefix filter
      limit: max returned parts (default 1000)
      guard: max visited objects (default 60000)

    Groups are traversed but never centered. Symbol instances and Structural
    Member PIOs are terminal parts. Mesh centers are read only after a cheap
    Get3DInfo filter identifies simple panels/straps/tubes. Centroid3D is never
    used.
    """
    layer_name = str(p.get('layer') or '').strip()
    if not layer_name:
        return {'error': 'layer is required'}
    layer_h = _safe(lambda: vs.GetLayerByName(layer_name))
    if not layer_h:
        return {'error': 'layer not found: %s' % layer_name}
    limit = max(1, int(p.get('limit', 1000)))
    guard = max(limit, int(p.get('guard', 60000)))
    region = p.get('region') if isinstance(p.get('region'), dict) else None
    if region:
        required = ('x1', 'y1', 'x2', 'y2')
        if not all(k in region for k in required):
            return {'error': 'region requires x1, y1, x2, y2'}
        try:
            rx1, ry1 = float(region['x1']), float(region['y1'])
            rx2, ry2 = float(region['x2']), float(region['y2'])
        except Exception:
            return {'error': 'region coordinates must be numeric'}
        region = {'x1': min(rx1, rx2), 'y1': min(ry1, ry2),
                  'x2': max(rx1, rx2), 'y2': max(ry1, ry2)}
    roles = p.get('roles')
    if isinstance(roles, str): roles = [roles]
    roles = set(roles or [])
    profiles = p.get('profiles')
    if isinstance(profiles, str): profiles = [profiles]
    profiles = set(profiles or [])
    name_prefix = str(p.get('name_prefix') or '')
    objects, errors = [], []
    visited = [0]
    containers = [0]
    truncated = [False]
    units = _safe(lambda: vs.GetUnits())
    units_per_inch = 1.0
    try:
        units_per_inch = abs(float(units[3])) or 1.0
    except Exception:
        pass

    def in_region(c):
        if not region: return True
        if not c: return False
        return (region['x1'] <= c['x'] <= region['x2'] and
                region['y1'] <= c['y'] <= region['y2'])

    def simple_mesh_candidate(dims):
        """Prefilter mesh leaves before the comparatively costly centroid call.

        Includes thin panels/straps and elongated members whose middle/cross-
        section dimension is <=4 inches; rejects crate/person/wheel-scale
        meshes. Thresholds are converted to the document's current units.
        """
        ordered = sorted((dims or {}).values())
        if len(ordered) != 3: return False
        thin = ordered[0] <= 0.2 * units_per_inch
        long_enough = ordered[2] >= 4.0 * units_per_inch
        slender = (ordered[1] <= 4.0 * units_per_inch and
                   ordered[2] / max(ordered[1], 1e-9) >= 2.5)
        return long_enough and (thin or slender)

    def build_part(h, depth, parent_id):
        t = int(_safe(lambda: vs.GetTypeN(h), 0) or 0)
        name = _safe(lambda: vs.GetName(h)) or ''
        rec_name = _pio_record(h)
        profile = (_safe(lambda: vs.GetRField(h, rec_name, 'ProfileSymbol')) or '') if rec_name else ''
        symbol_name = (_safe(lambda: vs.GetSymName(h)) or '') if t == 15 else ''
        dims, raw_dims = _dims3(h)

        # Only treat meshes as parts when they are cheap, simple panel/strap
        # shapes. Complex crate/person/wheel meshes remain represented by their
        # containing symbol instance and are never centroided here.
        if t == 40:
            if not simple_mesh_candidate(dims):
                return None

        # Type 84 is a solid NURBS/CSG result (for example CreateLoftSurfaces
        # with solid=True). It is a terminal part with reliable Get3DCntr /
        # Get3DInfo readback and must not disappear from revision snapshots.
        is_leaf_part = _is_assembly_leaf_part(profile, symbol_name, t)
        if not is_leaf_part:
            return None
        center = _center3(h)
        if not center:
            errors.append({'object_id': _oid(h),
                           'error': 'Get3DCntr returned no usable center'})
            return None
        if not in_region(center):
            return None
        role = _assembly_role(profile, symbol_name, t, dims, units_per_inch)
        if roles and role not in roles: return None
        if profiles and profile not in profiles and symbol_name not in profiles: return None
        if name_prefix and not name.startswith(name_prefix): return None
        axis = _primary_axis(dims)
        pio = {}
        if rec_name:
            for field in ('MemberID', 'ProfileShape', 'ProfileAngle',
                          'StartOffset', 'EndOffset', 'StartMiter', 'EndMiter',
                          'StartBevel', 'EndBevel', 'MemberMaterial',
                          'MemberTexture_3D'):
                value = _safe(lambda field=field: vs.GetRField(h, rec_name, field))
                if value not in (None, ''):
                    pio[field] = value
        return {
            'object_id': _oid(h),
            'name': name,
            'type': t,
            'type_name': OBJ_TYPES.get(t, 'type_%s' % t),
            'class': _safe(lambda: vs.GetClass(h)) or '',
            'layer': layer_name,
            'depth': depth,
            'parent_id': parent_id,
            'plugin_name': rec_name,
            'symbol_name': symbol_name,
            'profile': profile,
            'role': role,
            'axis': axis,
            'center': center,
            'dimensions': dims,
            'dimensions_raw_yxz': raw_dims,
            'length': (dims or {}).get(axis) if axis else None,
            'joint': {
                'start_offset': pio.get('StartOffset'),
                'end_offset': pio.get('EndOffset'),
                'start_miter': pio.get('StartMiter'),
                'end_miter': pio.get('EndMiter'),
                'start_bevel': pio.get('StartBevel'),
                'end_bevel': pio.get('EndBevel'),
            } if pio else None,
            'material': pio.get('MemberMaterial') if pio else None,
            'texture': pio.get('MemberTexture_3D') if pio else None,
            'pio': pio or None,
        }

    def walk(h, depth=0, parent_id=None):
        while h and visited[0] < guard:
            if truncated[0]: return
            visited[0] += 1
            t = int(_safe(lambda: vs.GetTypeN(h), 0) or 0)
            oid = _oid(h)
            # In this file type 11 is a group. Descend groups only; symbol
            # instances/PIOs are semantic parts and expanding them duplicates
            # their generated/internal geometry in the manifest.
            if t == 11:
                containers[0] += 1
                child = _safe(lambda h=h: vs.FInGroup(h))
                if child: walk(child, depth + 1, oid)
                if truncated[0]: return
            else:
                try:
                    part = build_part(h, depth, parent_id)
                    if part:
                        objects.append(part)
                        if len(objects) >= limit:
                            truncated[0] = True
                            return
                except Exception as exc:
                    errors.append({'object_id': oid, 'error': str(exc)})
            h = _safe(lambda h=h: vs.NextObj(h))

    walk(_safe(lambda: vs.FInLayer(layer_h)))
    objects.sort(key=lambda r: (
        _float_or((r.get('center') or {}).get('z')),
        _float_or((r.get('center') or {}).get('y')),
        _float_or((r.get('center') or {}).get('x')),
        r.get('role') or '', r.get('name') or '', r.get('object_id') or ''))
    return {
        'status': 'ok',
        'layer': layer_name,
        'region': region,
        'objects': objects,
        'count': len(objects),
        'objects_visited': visited[0],
        'containers_visited': containers[0],
        'units_per_inch': units_per_inch,
        'truncated_by_limit': bool(truncated[0]),
        'truncated_by_guard': bool(visited[0] >= guard),
        'truncated': bool(truncated[0] or visited[0] >= guard),
        'errors': errors,
    }


def _manifest_rows(value):
    if isinstance(value, dict):
        if 'objects' not in value:
            raise ValueError("manifest dict requires an 'objects' list")
        value = value.get('objects')
    if not isinstance(value, list):
        raise ValueError('manifest must be a row list or manifest dict')
    return value


def _path_value(row, field):
    cur = row
    for key in str(field).split('.'):
        if not isinstance(cur, dict): return None
        cur = cur.get(key)
    return cur


def _values_equal(expected, actual, tolerance):
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) <= tolerance
    if isinstance(expected, dict) and isinstance(actual, dict):
        return all(k in actual and _values_equal(v, actual[k], tolerance)
                   for k, v in expected.items())
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        return len(expected) == len(actual) and all(
            _values_equal(a, b, tolerance) for a, b in zip(expected, actual))
    return expected == actual


def get_assembly_diff(p):
    """Compare expected and actual assembly rows without touching the document.

    Params: expected, actual (list or manifest dict), tolerance (default .01),
    match_by (default ['name']), compare_fields (defaults to profile/role/axis/
    center/dimensions). Empty expected values are ignored during comparison.
    """
    try:
        expected = _manifest_rows(p.get('expected'))
        actual = _manifest_rows(p.get('actual'))
    except ValueError as exc:
        return {'error': str(exc)}
    tolerance = abs(_float_or(p.get('tolerance'), 0.01))
    match_by = p.get('match_by') or ['name']
    if isinstance(match_by, str): match_by = [match_by]
    compare_fields = p.get('compare_fields') or [
        'profile', 'symbol_name', 'role', 'axis', 'center', 'dimensions']
    if isinstance(compare_fields, str): compare_fields = [compare_fields]
    used = set()
    missing, mismatched, ambiguous = [], [], []

    for exp in expected:
        candidates = []
        for i, act in enumerate(actual):
            if i in used: continue
            if all(_path_value(exp, f) not in (None, '') and
                   _path_value(exp, f) == _path_value(act, f) for f in match_by):
                candidates.append((i, act))
        if not candidates:
            missing.append(exp)
            continue
        if len(candidates) > 1:
            ambiguous.append({
                'match': {f: _path_value(exp, f) for f in match_by},
                'candidate_count': len(candidates),
                'candidate_ids': [row.get('object_id') for _, row in candidates],
            })
        idx, act = candidates[0]
        used.add(idx)
        field_diffs = []
        for field in compare_fields:
            ev = _path_value(exp, field)
            if ev in (None, ''): continue
            av = _path_value(act, field)
            if not _values_equal(ev, av, tolerance):
                field_diffs.append({'field': field, 'expected': ev, 'actual': av})
        if field_diffs:
            mismatched.append({'match': {f: _path_value(exp, f) for f in match_by},
                               'expected': exp, 'actual': act,
                               'differences': field_diffs})
    extra = [row for i, row in enumerate(actual) if i not in used]
    matched = len(expected) - len(missing)
    return {
        'status': 'ok',
        'match_by': match_by,
        'compare_fields': compare_fields,
        'tolerance': tolerance,
        'missing': missing,
        'extra': extra,
        'mismatched': mismatched,
        'ambiguous_matches': ambiguous,
        'summary': {
            'expected': len(expected), 'actual': len(actual),
            'matched': matched,
            'exact': matched - len(mismatched),
            'missing': len(missing), 'extra': len(extra),
            'mismatched': len(mismatched),
            'ambiguous': len(ambiguous),
            'clean': not missing and not extra and not mismatched and not ambiguous,
        },
    }


# ── Guarded reference → native frame rebuild ────────────────────────────────

# StructuralMember record fields intentionally copied from a verified VW2026
# source member. Geometry/elevation/path fields are excluded: the new axis path
# and entity matrix own those values. Re-audit this list on a VW major upgrade.
_NATIVE_MEMBER_FIELDS = (
    'MemberID', 'MemberType', 'ProfileShape', 'ProfileSeries', 'ProfileSize',
    'ProfileSymbol', 'MajorBreadth', 'MinorBreadth', 'MajorDepth', 'MinorDepth',
    'StructuralUse', 'OtherStructuralUse', 'AxisAlign', 'OffsetY', 'OffsetZ',
    'MemberMaterial', 'MemberClass_3D', 'MemberAttributes_3D',
    'MemberTextureByClass_3D', 'MemberTexture_3D', 'MemberMergeInSections',
    'B', 'B1', 'D', 'D1', 'StartCap', 'EndCap', 'AttributesMode',
    'AttributesMode3D')


def _native_rebuild_tokens(value):
    if value is None: return []
    if isinstance(value, str): return [value]
    return [str(v) for v in value if str(v)]


def _native_rebuild_region(value):
    if not value: return None
    if not isinstance(value, dict):
        raise ValueError('region must be a dict')
    required = ('x1', 'y1', 'x2', 'y2')
    if not all(k in value for k in required):
        raise ValueError('region requires x1, y1, x2, y2')
    r = {k: float(value[k]) for k in required}
    out = {'x1': min(r['x1'], r['x2']), 'x2': max(r['x1'], r['x2']),
           'y1': min(r['y1'], r['y2']), 'y2': max(r['y1'], r['y2'])}
    if 'z1' in value or 'z2' in value:
        if 'z1' not in value or 'z2' not in value:
            raise ValueError('region Z filter requires both z1 and z2')
        z1, z2 = float(value['z1']), float(value['z2'])
        out.update({'z1': min(z1, z2), 'z2': max(z1, z2)})
    return out


def _native_rebuild_in_region(center, region):
    if not region: return True
    if not center: return False
    if not (region['x1'] <= center['x'] <= region['x2'] and
            region['y1'] <= center['y'] <= region['y2']):
        return False
    return ('z1' not in region or
            region['z1'] <= center['z'] <= region['z2'])


def _native_rebuild_rules(value):
    if isinstance(value, dict):
        return [{'contains': k, 'profile': v} for k, v in value.items()]
    if not isinstance(value, list):
        raise ValueError('profile_rules must be a list or dict')
    out = []
    for row in value:
        if not isinstance(row, dict) or not row.get('profile'):
            raise ValueError('each profile rule requires profile plus contains/starts_with')
        if not row.get('contains') and not row.get('starts_with'):
            raise ValueError('each profile rule requires contains or starts_with')
        out.append({'contains': str(row.get('contains') or ''),
                    'starts_with': str(row.get('starts_with') or ''),
                    'profile': str(row['profile'])})
    return out


def _native_rebuild_profile(name, rules):
    for rule in rules:
        if rule.get('starts_with') and name.startswith(rule['starts_with']):
            return rule['profile']
        if rule.get('contains') and rule['contains'] in name:
            return rule['profile']
    return ''


def _native_rebuild_source(source_map, profile, axis):
    for key in ('%s|%s' % (profile, axis), '%s:%s' % (profile, axis)):
        if key in source_map: return source_map[key]
    if axis == 'z':
        for fallback in ('x', 'y'):
            for key in ('%s|%s' % (profile, fallback),
                        '%s:%s' % (profile, fallback)):
                if key in source_map: return source_map[key]
    return None


def _native_rebuild_resolve_object(value):
    if not value: return None
    h = _h(value)
    if h: return h
    return _safe(lambda: vs.GetObject(str(value)))


def _native_rebuild_angle(value):
    try: return float(str(value).replace('\u00b0', '').strip())
    except Exception: return 0.0


def _native_rebuild_plan_summary(plan, include_items=False):
    out = {
        'status': plan.get('status'), 'plan_id': plan.get('plan_id'),
        'confirm_token': plan.get('confirm_token'),
        'layer': plan.get('layer'), 'reference_prefix': plan.get('reference_prefix'),
        'output_prefix': plan.get('output_prefix'),
        'item_count': len(plan.get('items') or []),
        'created_count': len(plan.get('created') or []),
        'tolerance': plan.get('tolerance'),
        'vertical_mode': plan.get('vertical_mode'),
        'summary': plan.get('summary') or {},
        'verification': plan.get('verification'),
    }
    if include_items: out['items'] = plan.get('items') or []
    return out


def _native_rebuild_get_plan(plan_id):
    return _NATIVE_REBUILD_PLANS.get(str(plan_id or ''))


def _native_rebuild_require_token(plan, token):
    return bool(plan and token and str(token) == str(plan.get('confirm_token')))


def _native_rebuild_validate_references(plan):
    errors = []
    tolerance = plan['tolerance']
    for item in plan['items']:
        h = _h(item['reference_id'])
        if not h:
            errors.append({'name': item['reference_name'], 'error': 'reference missing'})
            continue
        if (_safe(lambda: vs.GetName(h)) or '') != item['reference_name']:
            errors.append({'name': item['reference_name'], 'error': 'reference renamed'})
            continue
        obj_type = int(_safe(lambda: vs.GetTypeN(h), 0) or 0)
        if obj_type != item['reference_type']:
            errors.append({'name': item['reference_name'],
                           'error': 'reference type changed',
                           'expected': item['reference_type'], 'actual': obj_type})
            continue
        dims, _ = _dims3(h)
        center = _center3(h)
        if not dims or not center:
            errors.append({'name': item['reference_name'],
                           'error': 'reference dimensions/center unreadable'})
            continue
        if any(abs(dims[k] - item['dimensions'][k]) > tolerance
               for k in ('x', 'y', 'z')):
            errors.append({'name': item['reference_name'],
                           'error': 'reference dimensions changed',
                           'expected': item['dimensions'], 'actual': dims})
        if any(abs(center[k] - item['center'][k]) > tolerance
               for k in ('x', 'y', 'z')):
            errors.append({'name': item['reference_name'],
                           'error': 'reference center changed',
                           'expected': item['center'], 'actual': center})
    return errors


def plan_native_frame_rebuild(p):
    """Plan a guarded reference-to-native profile rebuild without document mutation.

    Required: layer, reference_prefix, output_prefix, expected_count,
    profile_rules, source_map. Optional region supports x/y and z bounds;
    include_name_contains is ANY-match; exclude_name_contains is ANY-match.
    If either z1 or z2 is supplied, both are required. Sources resolve by
    semantic object name first-class (UUID also accepted).
    """
    layer_name = str(p.get('layer') or '').strip()
    reference_prefix = str(p.get('reference_prefix') or '')
    output_prefix = str(p.get('output_prefix') or '')
    if not layer_name: return {'error': 'layer is required'}
    if not reference_prefix: return {'error': 'reference_prefix is required'}
    if not output_prefix: return {'error': 'output_prefix is required'}
    try: expected_count = int(p.get('expected_count'))
    except Exception: return {'error': 'expected_count is required and must be an integer'}
    if expected_count < 1: return {'error': 'expected_count must be >= 1'}
    layer_h = _safe(lambda: vs.GetLayerByName(layer_name))
    if not layer_h: return {'error': 'layer not found: %s' % layer_name}
    try:
        region = _native_rebuild_region(p.get('region'))
        rules = _native_rebuild_rules(p.get('profile_rules'))
    except ValueError as exc:
        return {'error': str(exc)}
    source_map = p.get('source_map')
    if not isinstance(source_map, dict) or not source_map:
        return {'error': 'source_map must be a non-empty dict keyed profile|axis'}
    include = _native_rebuild_tokens(p.get('include_name_contains'))
    exclude = _native_rebuild_tokens(p.get('exclude_name_contains'))
    orientation_profiles = set(_native_rebuild_tokens(p.get('orientation_profiles')))
    tolerance = abs(_float_or(p.get('tolerance'), 0.01))
    vertical_mode = str(p.get('vertical_mode') or 'profile_extrude')
    if vertical_mode != 'profile_extrude':
        return {'error': "vertical_mode currently supports only 'profile_extrude'"}
    material_override = str(p.get('material_name') or '')
    texture_override = str(p.get('texture_name') or '')
    target_class_override = p.get('target_class')
    targets, scan_errors = [], []
    visited = [0]

    def eligible(name, center):
        if not name.startswith(reference_prefix): return False
        if include and not any(token in name for token in include): return False
        if exclude and any(token in name for token in exclude): return False
        return _native_rebuild_in_region(center, region)

    def walk(h):
        while h:
            visited[0] += 1
            t = int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0)
            if t == 11:
                child = _safe(lambda h=h: vs.FInGroup(h))
                if child: walk(child)
            else:
                name = _safe(lambda h=h: vs.GetName(h)) or ''
                if name.startswith(reference_prefix):
                    dims, _ = _dims3(h)
                    center = _center3(h) if dims else None
                    if eligible(name, center):
                        if not dims or not center:
                            scan_errors.append({'name': name,
                                                'error': 'dimensions/center unreadable'})
                        else:
                            targets.append((h, name, t, dims, center))
            h = _safe(lambda h=h: vs.NextObj(h))

    walk(_safe(lambda: vs.FInLayer(layer_h)))
    targets.sort(key=lambda row: row[1])
    if scan_errors:
        return {'error': 'target scan failed', 'details': scan_errors}
    if len(targets) != expected_count:
        return {'error': 'expected_count mismatch', 'expected_count': expected_count,
                'actual_count': len(targets),
                'target_names': [row[1] for row in targets]}

    items, errors, source_cache = [], [], {}
    output_names = set()
    for h, name, obj_type, dims, center in targets:
        profile = _native_rebuild_profile(name, rules)
        axis = _primary_axis(dims)
        output_name = output_prefix + name[len(reference_prefix):]
        if not profile:
            errors.append({'name': name, 'error': 'no profile rule matched'})
            continue
        if output_name in output_names:
            errors.append({'name': name, 'error': 'duplicate output name: %s' % output_name})
            continue
        output_names.add(output_name)
        if _safe(lambda: vs.GetObject(output_name)):
            errors.append({'name': name, 'error': 'output name already exists: %s' % output_name})
            continue
        source_value = _native_rebuild_source(source_map, profile, axis)
        source_h = _native_rebuild_resolve_object(source_value)
        if not source_h:
            errors.append({'name': name,
                           'error': 'source missing for %s|%s' % (profile, axis)})
            continue
        source_id = _oid(source_h)
        if source_id not in source_cache:
            rec_name = _pio_record(source_h)
            source_profile = (_safe(lambda: vs.GetRField(
                source_h, rec_name, 'ProfileSymbol')) or '') if rec_name else ''
            source_dims, _ = _dims3(source_h)
            source_cache[source_id] = {
                'handle': source_h, 'record': rec_name, 'profile': source_profile,
                'axis': _primary_axis(source_dims),
                'name': _safe(lambda: vs.GetName(source_h)) or '',
                'material': (_safe(lambda: vs.GetRField(
                    source_h, rec_name, 'MemberMaterial')) or '') if rec_name else '',
                'texture': (_safe(lambda: vs.GetRField(
                    source_h, rec_name, 'MemberTexture_3D')) or '') if rec_name else '',
                'profile_angle': (_safe(lambda: vs.GetRField(
                    source_h, rec_name, 'ProfileAngle')) or '0\u00b0') if rec_name else '0\u00b0',
            }
        source = source_cache[source_id]
        if not source['record'] or source['profile'] != profile:
            errors.append({'name': name, 'error': 'source profile mismatch',
                           'expected': profile, 'actual': source['profile'],
                           'source': source['name']})
            continue
        mode = 'profile_extrude' if axis == 'z' else 'structural_member'
        if mode == 'structural_member' and source['axis'] != axis:
            errors.append({'name': name, 'error': 'source axis mismatch',
                           'expected': axis, 'actual': source['axis'],
                           'source': source['name']})
            continue
        if mode == 'profile_extrude':
            profile_def = _safe(lambda: vs.GetObject(profile))
            if not profile_def or not _safe(lambda: vs.FInSymDef(profile_def)):
                errors.append({'name': name,
                               'error': 'profile symbol definition not found: %s' % profile})
                continue
        angle = source['profile_angle']
        if profile in orientation_profiles or profile.upper().startswith('L_'):
            # Native Structural Members already carry the authoritative roll in
            # ProfileAngle. Imported symbol/mesh references do not, so derive a
            # quarter-turn roll from their 3D orientation as the fallback.
            reference_rec = _pio_record(h)
            reference_angle = (_safe(lambda: vs.GetRField(
                h, reference_rec, 'ProfileAngle')) or '') if reference_rec else ''
            if reference_angle:
                angle = reference_angle
            else:
                orientation = _safe(lambda: vs.Get3DOrientation(h))
                if isinstance(orientation, (tuple, list)) and len(orientation) > 1:
                    angle = '%g\u00b0' % ((round(abs(float(orientation[1])) / 90.0) * 90) % 360)
        items.append({
            'reference_id': _oid(h), 'reference_name': name,
            'reference_type': obj_type, 'output_name': output_name,
            'profile': profile, 'axis': axis, 'mode': mode,
            'center': center, 'dimensions': dims,
            'source_id': source_id, 'source_name': source['name'],
            'profile_angle': angle,
            'material': material_override or source['material'],
            'texture': texture_override or source['texture'],
            'target_class': (str(target_class_override) if target_class_override is not None
                             else (_safe(lambda h=h: vs.GetClass(h)) or '')),
        })
    if errors:
        return {'error': 'plan validation failed', 'details': errors,
                'valid_items': len(items), 'expected_count': expected_count}

    by_mode, by_profile_axis = {}, {}
    for item in items:
        by_mode[item['mode']] = by_mode.get(item['mode'], 0) + 1
        key = '%s|%s' % (item['profile'], item['axis'])
        by_profile_axis[key] = by_profile_axis.get(key, 0) + 1
    plan_id = 'native-%s' % uuid.uuid4().hex[:12]
    plan = {
        'plan_id': plan_id, 'confirm_token': uuid.uuid4().hex,
        'status': 'planned', 'created_at': time.time(), 'layer': layer_name,
        'reference_prefix': reference_prefix, 'output_prefix': output_prefix,
        'region': region, 'items': items, 'created': [],
        'tolerance': tolerance, 'vertical_mode': vertical_mode,
        'summary': {'by_mode': by_mode, 'by_profile_axis': by_profile_axis,
                    'objects_visited': visited[0]},
        'verification': None,
    }
    _NATIVE_REBUILD_PLANS[plan_id] = plan
    # Keep bounded in a long VW session; only completed plans are pruned.
    if len(_NATIVE_REBUILD_PLANS) > 20:
        completed = sorted((v for v in _NATIVE_REBUILD_PLANS.values()
                            if v.get('status') in ('committed', 'rolled_back')),
                           key=lambda v: v.get('created_at', 0))
        for old in completed[:len(_NATIVE_REBUILD_PLANS) - 20]:
            _NATIVE_REBUILD_PLANS.pop(old['plan_id'], None)
    result = _native_rebuild_plan_summary(plan, include_items=True)
    result['dry_run'] = True
    result['next_action'] = 'execute_native_frame_rebuild with plan_id + confirm_token'
    return result


def _native_rebuild_copy_member_fields(source_h, target_h, item):
    source_rec = _pio_record(source_h)
    target_rec = _pio_record(target_h)
    if not source_rec or not target_rec:
        raise RuntimeError('Structural Member record unavailable')
    for field in _NATIVE_MEMBER_FIELDS:
        value = _safe(lambda field=field: vs.GetRField(source_h, source_rec, field))
        if value is not None:
            vs.SetRField(target_h, target_rec, field, str(value))
    for field, value in (
            ('StartOffset', '0'), ('EndOffset', '0'),
            ('StartMiter', '0\u00b0'), ('EndMiter', '0\u00b0'),
            ('StartBevel', '0\u00b0'), ('EndBevel', '0\u00b0'),
            ('ProfileAngle', item['profile_angle'])):
        vs.SetRField(target_h, target_rec, field, value)
    if item.get('material'):
        vs.SetRField(target_h, target_rec, 'MemberMaterial', item['material'])
    if item.get('texture'):
        vs.SetRField(target_h, target_rec, 'MemberTexture_3D', item['texture'])
    return target_rec


def _native_rebuild_make_path(center, axis, length):
    p0 = [center['x'], center['y'], center['z']]
    p1 = [center['x'], center['y'], center['z']]
    idx = {'x': 0, 'y': 1, 'z': 2}[axis]
    p0[idx] -= length / 2.0
    p1[idx] += length / 2.0
    vs.BeginPoly3D()
    vs.Add3DPt(tuple(p0))
    vs.Add3DPt(tuple(p1))
    vs.EndPoly3D()
    poly = _safe(lambda: vs.LNewObj())
    if not poly: raise RuntimeError('3D path polygon creation failed')
    path = _safe(lambda: vs.ConvertToNURBS(poly, False))
    if not path: raise RuntimeError('path NURBS conversion failed')
    return path


def _native_rebuild_create_pio(item, layer_h):
    length = item['dimensions'][item['axis']]
    path = _native_rebuild_make_path(item['center'], item['axis'], length)
    h = _safe(lambda: vs.CreateCustomObjectPath('StructuralMember', path, None))
    if not h:
        _safe(lambda: vs.DelObject(path))
        raise RuntimeError('CreateCustomObjectPath failed')
    try:
        source_h = _h(item['source_id'])
        if not source_h: raise RuntimeError('source disappeared before create')
        _native_rebuild_copy_member_fields(source_h, h, item)
        if not _safe(lambda: vs.SetParent(h, layer_h), False):
            raise RuntimeError('SetParent failed')
        if item.get('target_class'):
            vs.SetClass(h, item['target_class'])
        vs.SetName(h, item['output_name'])
        # ResetObject is queued until VW gets another event-pump cycle. Applying
        # the elevation here appears to work immediately, but that queued reset
        # subsequently restores Z=0. execute_native_frame_rebuild therefore has
        # a second, explicit placement pass in a later MCP call.
        vs.ResetObject(h)
        return h
    except Exception:
        _safe(lambda: vs.DelObject(h))
        raise


def _native_rebuild_create_extrude(item, layer_h):
    profile_def = _safe(lambda: vs.GetObject(item['profile']))
    profile_source = _safe(lambda: vs.FInSymDef(profile_def)) if profile_def else None
    if not profile_source:
        raise RuntimeError('profile definition unavailable: %s' % item['profile'])
    profile = _safe(lambda: vs.HDuplicate(profile_source, 0, 0))
    if not profile: raise RuntimeError('profile duplication failed')
    extrude = None
    try:
        if not _safe(lambda: vs.SetParent(profile, layer_h), False):
            raise RuntimeError('profile SetParent failed')
        bbox = _safe(lambda: vs.GetBBox(profile))
        if not bbox: raise RuntimeError('profile bounding box unavailable')
        pcx = (float(bbox[0][0]) + float(bbox[1][0])) / 2.0
        pcy = (float(bbox[0][1]) + float(bbox[1][1])) / 2.0
        vs.HMove(profile, item['center']['x'] - pcx, item['center']['y'] - pcy)
        length = item['dimensions']['z']
        bottom = item['center']['z'] - length / 2.0
        top = item['center']['z'] + length / 2.0
        extrude = _safe(lambda: vs.HExtrude(profile, bottom, top))
        if not extrude: raise RuntimeError('HExtrude failed')
        vs.SetParent(extrude, layer_h)
        if item.get('target_class'):
            vs.SetClass(extrude, item['target_class'])
        vs.SetName(extrude, item['output_name'])
        if item.get('material'):
            material_h = _safe(lambda: vs.GetObject(item['material']))
            if not material_h:
                raise RuntimeError('material not found: %s' % item['material'])
            vs.SetObjMaterialHandle(extrude, material_h)
        return extrude
    except Exception:
        if extrude: _safe(lambda: vs.DelObject(extrude))
        else: _safe(lambda: vs.DelObject(profile))
        raise


def execute_native_frame_rebuild(p):
    """Additively create/finalize a planned native frame; never delete originals.

    Structural Member regeneration is deferred by Vectorworks. The first call
    creates and queues regeneration; repeat this action with the same plan/token
    in a later MCP call to persist PIO elevation before verification.
    """
    plan = _native_rebuild_get_plan(p.get('plan_id'))
    if not plan: return {'error': 'plan not found or expired after VW restart'}
    if not _native_rebuild_require_token(plan, p.get('confirm_token')):
        return {'error': 'confirm_token does not match plan'}
    if plan['status'] == 'created_pending_placement':
        created_by_ref = {row['reference_id']: row for row in plan.get('created') or []}
        placement_errors = []
        for item in plan['items']:
            if item['mode'] != 'structural_member':
                continue
            created = created_by_ref.get(item['reference_id'])
            h = _h(created.get('object_id')) if created else None
            if not h and created:
                h = _safe(lambda created=created: vs.GetObject(created['name']))
            if not h:
                placement_errors.append({'name': item['output_name'],
                                         'error': 'created object missing'})
                continue
            matrix = _safe(lambda h=h: vs.GetEntityMatrix(h))
            if not isinstance(matrix, (tuple, list)) or len(matrix) < 5 or not matrix[0]:
                placement_errors.append({'name': item['output_name'],
                                         'error': 'GetEntityMatrix failed'})
                continue
            offset = matrix[1]
            ok = _safe(lambda h=h, matrix=matrix, offset=offset, item=item:
                       vs.SetEntityMatrix(
                           h, (float(offset[0]), float(offset[1]),
                               float(item['center']['z'])),
                           float(matrix[2]), float(matrix[3]), float(matrix[4])), False)
            if not ok:
                placement_errors.append({'name': item['output_name'],
                                         'error': 'SetEntityMatrix failed'})
        if placement_errors:
            plan['status'] = 'placement_failed'
            plan['verification'] = {'clean': False, 'placement_errors': placement_errors}
            return {'error': 'PIO placement failed', 'status': plan['status'],
                    'plan_id': plan['plan_id'], 'details': placement_errors}
        plan['status'] = 'created_pending_verification'
        _safe(lambda: vs.ReDrawAll())
        result = _native_rebuild_plan_summary(plan)
        result['next_action'] = 'verify_native_frame_rebuild in a new MCP call'
        return result
    if plan['status'] != 'planned':
        return {'error': 'plan status must be planned or created_pending_placement',
                'status': plan['status']}
    reference_errors = _native_rebuild_validate_references(plan)
    if reference_errors:
        return {'error': 'references changed since plan', 'details': reference_errors}
    for item in plan['items']:
        if _safe(lambda item=item: vs.GetObject(item['output_name'])):
            return {'error': 'output name now exists: %s' % item['output_name']}
    layer_h = _safe(lambda: vs.GetLayerByName(plan['layer']))
    if not layer_h: return {'error': 'target layer disappeared: %s' % plan['layer']}
    _safe(lambda: vs.NameUndoEvent('Agent: additive native frame rebuild'))
    created = []
    try:
        for item in plan['items']:
            h = (_native_rebuild_create_extrude(item, layer_h)
                 if item['mode'] == 'profile_extrude'
                 else _native_rebuild_create_pio(item, layer_h))
            created.append({'object_id': _oid(h), 'name': item['output_name'],
                            'mode': item['mode'], 'reference_id': item['reference_id']})
        plan['created'] = created
        plan['status'] = 'created_pending_placement'
        plan['verification'] = None
        _safe(lambda: vs.ReDrawAll())
        result = _native_rebuild_plan_summary(plan)
        result.update({'created': created,
                       'next_action': ('repeat execute_native_frame_rebuild with the same '
                                       'plan_id + confirm_token in a new MCP call to persist '
                                       'PIO elevation after deferred regeneration')})
        return result
    except Exception as exc:
        for row in created:
            h = _h(row.get('object_id')) or _safe(lambda row=row: vs.GetObject(row['name']))
            if h: _safe(lambda h=h: vs.DelObject(h))
        remaining = []
        for row in created:
            h = _h(row.get('object_id')) or _safe(lambda row=row: vs.GetObject(row['name']))
            if h: remaining.append(row)
        plan['created'] = remaining
        plan['status'] = ('failed_partial_rollback' if remaining
                          else 'failed_rolled_back')
        plan['verification'] = {'clean': False, 'error': str(exc),
                                'remaining_created': remaining}
        _safe(lambda: vs.ReDrawAll())
        return {'error': str(exc), 'status': plan['status'],
                'created_then_removed': len(created) - len(remaining),
                'remaining_created': remaining, 'plan_id': plan['plan_id']}


def _native_rebuild_verify(plan, update_status=True):
    tolerance = plan['tolerance']
    reference_errors = _native_rebuild_validate_references(plan)
    mismatches, actual = [], []
    created_by_ref = {row['reference_id']: row for row in plan.get('created') or []}
    for item in plan['items']:
        created = created_by_ref.get(item['reference_id'])
        h = _h(created.get('object_id')) if created else None
        if not h and created:
            h = _safe(lambda created=created: vs.GetObject(created['name']))
        if not h:
            mismatches.append({'name': item['output_name'], 'error': 'created object missing'})
            continue
        dims, _ = _dims3(h)
        center = _center3(h)
        obj_type = int(_safe(lambda: vs.GetTypeN(h), 0) or 0)
        row = {'name': item['output_name'], 'object_id': _oid(h), 'type': obj_type,
               'mode': item['mode'], 'profile': None, 'center': center,
               'dimensions': dims, 'material': None, 'texture': None}
        expected_type = 24 if item['mode'] == 'profile_extrude' else 86
        if obj_type != expected_type:
            mismatches.append({'name': item['output_name'], 'field': 'type',
                               'expected': expected_type, 'actual': obj_type})
        if not dims or not center:
            mismatches.append({'name': item['output_name'],
                               'error': 'dimensions/center unreadable'})
        else:
            for field in ('x', 'y', 'z'):
                if abs(dims[field] - item['dimensions'][field]) > tolerance:
                    mismatches.append({'name': item['output_name'],
                                       'field': 'dimensions.%s' % field,
                                       'expected': item['dimensions'][field],
                                       'actual': dims[field]})
                if abs(center[field] - item['center'][field]) > tolerance:
                    mismatches.append({'name': item['output_name'],
                                       'field': 'center.%s' % field,
                                       'expected': item['center'][field],
                                       'actual': center[field]})
        layer_name = _safe(lambda: vs.GetLName(vs.GetLayer(h))) or ''
        if layer_name != plan['layer']:
            mismatches.append({'name': item['output_name'], 'field': 'layer',
                               'expected': plan['layer'], 'actual': layer_name})
        if item['mode'] == 'structural_member':
            rec_name = _pio_record(h)
            row['profile'] = (_safe(lambda: vs.GetRField(h, rec_name, 'ProfileSymbol')) or '')
            row['material'] = (_safe(lambda: vs.GetRField(h, rec_name, 'MemberMaterial')) or '')
            row['texture'] = (_safe(lambda: vs.GetRField(h, rec_name, 'MemberTexture_3D')) or '')
            angle = _safe(lambda: vs.GetRField(h, rec_name, 'ProfileAngle')) or '0\u00b0'
            row['profile_angle'] = angle
            if row['profile'] != item['profile']:
                mismatches.append({'name': item['output_name'], 'field': 'profile',
                                   'expected': item['profile'], 'actual': row['profile']})
            if abs(_native_rebuild_angle(angle) -
                   _native_rebuild_angle(item['profile_angle'])) > 0.01:
                mismatches.append({'name': item['output_name'], 'field': 'profile_angle',
                                   'expected': item['profile_angle'], 'actual': angle})
            if item.get('material') and row['material'] != item['material']:
                mismatches.append({'name': item['output_name'], 'field': 'material',
                                   'expected': item['material'], 'actual': row['material']})
            if item.get('texture') and row['texture'] != item['texture']:
                mismatches.append({'name': item['output_name'], 'field': 'texture',
                                   'expected': item['texture'], 'actual': row['texture']})
        else:
            material_h = _safe(lambda: vs.GetObjMaterialHandle(h))
            row['profile'] = item['profile']
            row['material'] = (_safe(lambda: vs.GetName(material_h)) or '') if material_h else ''
            if item.get('material') and row['material'] != item['material']:
                mismatches.append({'name': item['output_name'], 'field': 'material',
                                   'expected': item['material'], 'actual': row['material']})
        actual.append(row)
    clean = (bool(plan['items']) and not reference_errors and not mismatches and
             len(actual) == len(plan['items']))
    verification = {
        'clean': clean, 'expected': len(plan['items']), 'actual': len(actual),
        'reference_errors': reference_errors, 'mismatches': mismatches,
        'verified_objects': actual,
    }
    if update_status:
        plan['verification'] = verification
        plan['status'] = 'verified' if clean else 'verification_failed'
    return verification


def verify_native_frame_rebuild(p):
    """Verify a created rebuild after Vectorworks has had a pump cycle to regenerate PIOs."""
    plan = _native_rebuild_get_plan(p.get('plan_id'))
    if not plan: return {'error': 'plan not found or expired after VW restart'}
    if plan['status'] not in ('created_pending_verification', 'verification_failed', 'verified'):
        return {'error': 'plan has no created objects to verify', 'status': plan['status']}
    verification = _native_rebuild_verify(plan, update_status=True)
    result = _native_rebuild_plan_summary(plan)
    result['verification'] = verification
    result['ready_to_commit'] = bool(verification['clean'])
    result['next_action'] = ('commit_native_frame_rebuild with plan_id + confirm_token'
                             if verification['clean'] else
                             'fix or rollback_native_frame_rebuild')
    return result


def commit_native_frame_rebuild(p):
    """Delete planned originals only after a clean fresh verification.

    Saves the named document immediately before deletion by default, then leaves
    the final deletion dirty for visual QA. Save explicitly after visual approval.
    """
    plan = _native_rebuild_get_plan(p.get('plan_id'))
    if not plan: return {'error': 'plan not found or expired after VW restart'}
    if not _native_rebuild_require_token(plan, p.get('confirm_token')):
        return {'error': 'confirm_token does not match plan'}
    if plan['status'] not in ('verified', 'verification_failed'):
        return {'error': 'plan must be verified before commit', 'status': plan['status']}
    verification = _native_rebuild_verify(plan, update_status=True)
    if not verification['clean']:
        return {'error': 'fresh verification failed; originals preserved',
                'status': plan['status'], 'verification': verification}
    if bool(p.get('save_checkpoint', True)):
        _safe(lambda: vs.DoMenuTextByName('Save', 0))
    _safe(lambda: vs.NameUndoEvent('Agent: commit native frame rebuild'))
    deleted = []
    for item in plan['items']:
        h = _h(item['reference_id'])
        if not h:
            return {'error': 'reference disappeared during commit; use Vectorworks Undo',
                    'deleted_before_error': deleted, 'final_save_required': False}
        vs.DelObject(h)
        deleted.append(item['reference_id'])
    remaining = [item['reference_id'] for item in plan['items']
                 if _h(item['reference_id'])]
    if remaining:
        return {'error': 'some originals remain; use Vectorworks Undo',
                'remaining_reference_ids': remaining,
                'deleted_count': len(deleted) - len(remaining),
                'final_save_required': False}
    plan['status'] = 'committed'
    plan['committed_at'] = time.time()
    _safe(lambda: vs.ReDrawAll())
    return {'status': 'committed', 'plan_id': plan['plan_id'],
            'deleted_originals': len(deleted),
            'created_native': len(plan.get('created') or []),
            'checkpoint_saved_before_delete': bool(p.get('save_checkpoint', True)),
            'final_save_required': True,
            'recovery': ('Until final save, use Vectorworks Undo to restore originals. '
                         'After visual QA, call save_document.')}


def rollback_native_frame_rebuild(p):
    """Remove additive native objects while originals are still present."""
    plan = _native_rebuild_get_plan(p.get('plan_id'))
    if not plan: return {'error': 'plan not found or expired after VW restart'}
    if not _native_rebuild_require_token(plan, p.get('confirm_token')):
        return {'error': 'confirm_token does not match plan'}
    if plan['status'] == 'committed':
        return {'error': ('committed plans cannot be programmatically rolled back; '
                          'use Vectorworks Undo or reopen the saved checkpoint')}
    _safe(lambda: vs.NameUndoEvent('Agent: rollback native frame rebuild'))
    removed, missing = [], []
    for row in plan.get('created') or []:
        h = _h(row.get('object_id')) or _safe(lambda row=row: vs.GetObject(row['name']))
        if h:
            vs.DelObject(h)
        else:
            missing.append(row.get('object_id'))
    remaining = []
    for row in plan.get('created') or []:
        h = _h(row.get('object_id')) or _safe(lambda row=row: vs.GetObject(row['name']))
        if h: remaining.append(row)
        else: removed.append(row.get('object_id'))
    if remaining:
        plan['created'] = remaining
        plan['status'] = 'failed_partial_rollback'
        return {'error': 'some created objects could not be removed',
                'status': plan['status'], 'plan_id': plan['plan_id'],
                'removed_created': len(removed), 'remaining_created': remaining,
                'originals_preserved': not bool(_native_rebuild_validate_references(plan))}
    plan['status'] = 'rolled_back'
    plan['rolled_back_at'] = time.time()
    plan['verification'] = None
    _safe(lambda: vs.ReDrawAll())
    return {'status': 'rolled_back', 'plan_id': plan['plan_id'],
            'removed_created': len(removed), 'already_missing': missing,
            'originals_preserved': not bool(_native_rebuild_validate_references(plan))}


def _normalize_object_kind(obj_type, name, obj_class, dims, center,
                           helper_name='', helper_class='NonPlot'):
    """Pure classifier used by the guarded assembly-normalization workflow."""
    if helper_name and name == helper_name: return 'helper'
    if helper_class and obj_class == helper_class: return 'helper'
    if int(obj_type or 0) in (25, 28): return 'helper'
    if not isinstance(dims, dict) or not isinstance(center, dict): return 'helper'
    vals = [dims.get(k) for k in ('x', 'y', 'z')] + [center.get(k) for k in ('x', 'y', 'z')]
    try:
        if not all(math.isfinite(float(v)) and abs(float(v)) < 1e12 for v in vals):
            return 'helper'
        if max(abs(float(dims[k])) for k in ('x', 'y', 'z')) <= 1e-6:
            return 'helper'
    except Exception:
        return 'helper'
    return 'model'


def _normalize_snapshot(h):
    dims, _ = _dims3(h)
    center = _center3(h) if dims else None
    return {
        'object_id': _oid(h), 'name': _safe(lambda: vs.GetName(h)) or '',
        'class': _safe(lambda: vs.GetClass(h)) or '',
        'type': int(_safe(lambda: vs.GetTypeN(h), 0) or 0),
        'dimensions': dims, 'center': center,
    }


def _normalize_semantic_count(h):
    t = int(_safe(lambda: vs.GetTypeN(h), 0) or 0)
    if t == 11:
        child = _safe(lambda: vs.FInGroup(h))
    elif t == 15:
        sym_name = _safe(lambda: vs.GetSymName(h)) or ''
        sym_def = _safe(lambda: vs.GetObject(sym_name)) if sym_name else None
        child = _safe(lambda: vs.FInSymDef(sym_def)) if sym_def else None
    else:
        return 1
    count = 0
    while child:
        count += 1
        child = _safe(lambda child=child: vs.NextObj(child))
    return count


def _normalize_collect_pios(handles):
    rows, seen = [], set()

    def capture(h):
        oid = _oid(h)
        if oid in seen: return
        seen.add(oid)
        matrix = _safe(lambda h=h: vs.GetEntityMatrix(h))
        center = _center3(h)
        if (isinstance(matrix, (tuple, list)) and len(matrix) >= 5 and matrix[0]
                and center):
            rows.append({
                'object_id': oid, 'name': _safe(lambda h=h: vs.GetName(h)) or '',
                'matrix': [list(matrix[1]), float(matrix[2]), float(matrix[3]),
                           float(matrix[4])],
                'center': center,
            })

    def walk(h):
        while h:
            t = int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0)
            if t == 11:
                child = _safe(lambda h=h: vs.FInGroup(h))
                if child: walk(child)
            elif t == 86:
                capture(h)
            h = _safe(lambda h=h: vs.NextObj(h))

    for h in handles:
        t = int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0)
        if t == 11: walk(_safe(lambda h=h: vs.FInGroup(h)))
        elif t == 86: capture(h)
    return rows


def _normalize_direct_on_layer(h, layer_h):
    return bool(h and _safe(lambda: vs.GetParent(h)) == layer_h)


def _normalize_pio_group_move_risk(action, container_type, pio_count):
    """Fail closed when normalization would leave editable PIOs in a movable group."""
    return bool(pio_count and (action == 'group' or int(container_type or 0) == 11))


def _normalize_new_group(layer_h, name, obj_class=''):
    # LNewObj *after* EndGroup is not reliable: on a non-active target layer it
    # can return a previous object and contaminate an unrelated container. Keep
    # the disposable child's handle while the group is open, then resolve its
    # actual parent after EndGroup.
    vs.BeginGroup(); vs.Locus(0, 0)
    child = _safe(lambda: vs.LNewObj())
    vs.EndGroup()
    group = _safe(lambda: vs.GetParent(child)) if child else None
    if not group: raise RuntimeError('group creation failed: %s' % name)
    if child: vs.DelObject(child)
    _safe(lambda: vs.SetParent(group, layer_h))
    if not _normalize_direct_on_layer(group, layer_h):
        vs.DelObject(group); raise RuntimeError('group SetParent failed: %s' % name)
    vs.SetName(group, name)
    if obj_class: vs.SetClass(group, obj_class)
    return group


def _normalize_validate_plan_objects(plan):
    errors, tolerance = [], plan['tolerance']
    for expected in plan['model_sources'] + plan['helper_sources']:
        h = _h(expected['object_id'])
        if not h:
            errors.append({'object_id': expected['object_id'], 'error': 'object missing'})
            continue
        actual = _normalize_snapshot(h)
        for field in ('name', 'class', 'type'):
            if actual.get(field) != expected.get(field):
                errors.append({'object_id': expected['object_id'], 'field': field,
                               'expected': expected.get(field), 'actual': actual.get(field)})
        if not actual.get('dimensions') or not actual.get('center'):
            errors.append({'object_id': expected['object_id'],
                           'error': 'dimensions/center unreadable'})
            continue
        for field in ('x', 'y', 'z'):
            if abs(actual['dimensions'][field] - expected['dimensions'][field]) > tolerance:
                errors.append({'object_id': expected['object_id'],
                               'field': 'dimensions.%s' % field})
            if abs(actual['center'][field] - expected['center'][field]) > tolerance:
                errors.append({'object_id': expected['object_id'],
                               'field': 'center.%s' % field})
    return errors


def plan_normalize_assembly(p):
    """Dry-run one layer into a named assembly + optional NonPlot helper group."""
    layer_name = str(p.get('layer') or '').strip()
    assembly_name = str(p.get('assembly_name') or '').strip()
    helper_name = str(p.get('helper_name') or (assembly_name + '-NONPLOT-HELPERS')).strip()
    helper_class = str(p.get('helper_class') or 'NonPlot').strip()
    if not layer_name: return {'error': 'layer is required'}
    if not assembly_name: return {'error': 'assembly_name is required'}
    try: expected_count = int(p.get('expected_model_count'))
    except Exception: return {'error': 'expected_model_count is required and must be an integer'}
    if expected_count < 1: return {'error': 'expected_model_count must be >= 1'}
    target_floor = _float_or(p.get('target_floor'), 0.0)
    tolerance = abs(_float_or(p.get('tolerance'), 0.01))
    layer_h = _safe(lambda: vs.GetLayerByName(layer_name))
    if not layer_h: return {'error': 'layer not found: %s' % layer_name}

    direct = []
    h = _safe(lambda: vs.FInLayer(layer_h))
    while h:
        direct.append(h)
        h = _safe(lambda h=h: vs.NextObj(h))
    existing = _safe(lambda: vs.GetObject(assembly_name))
    if existing and not _normalize_direct_on_layer(existing, layer_h):
        return {'error': 'assembly_name exists but is not top-level on target layer'}
    existing_helper = _safe(lambda: vs.GetObject(helper_name))
    if existing_helper and not _normalize_direct_on_layer(existing_helper, layer_h):
        return {'error': 'helper_name exists but is not top-level on target layer'}

    model_handles, helper_handles = [], []
    for obj in direct:
        if obj == existing_helper:
            continue
        snap = _normalize_snapshot(obj)
        kind = _normalize_object_kind(snap['type'], snap['name'], snap['class'],
                                      snap['dimensions'], snap['center'],
                                      helper_name, helper_class)
        (helper_handles if kind == 'helper' else model_handles).append(obj)
    if existing:
        stray_models = [h for h in model_handles if h != existing]
        if stray_models:
            return {'error': 'stray model geometry exists outside named assembly',
                    'object_ids': [_oid(h) for h in stray_models],
                    'object_names': [_safe(lambda h=h: vs.GetName(h)) or ''
                                     for h in stray_models]}
        model_handles = [existing]
    if not model_handles: return {'error': 'no model geometry found on target layer'}
    if existing:
        action = 'realign_existing'
        semantic_count = _normalize_semantic_count(existing)
    elif len(model_handles) == 1 and int(_safe(lambda: vs.GetTypeN(model_handles[0]), 0) or 0) in (11, 15):
        action = 'rename_existing'
        semantic_count = _normalize_semantic_count(model_handles[0])
    else:
        action = 'group'
        semantic_count = len(model_handles)
    if semantic_count != expected_count:
        return {'error': 'expected_model_count mismatch', 'expected': expected_count,
                'actual': semantic_count, 'action': action,
                'model_names': [_safe(lambda h=h: vs.GetName(h)) or '' for h in model_handles]}

    model_sources = [_normalize_snapshot(h) for h in model_handles]
    helper_sources = [_normalize_snapshot(h) for h in helper_handles]
    if any(not row['dimensions'] or not row['center'] for row in model_sources):
        return {'error': 'model geometry has unreadable dimensions/center'}
    floor_before = min(row['center']['z'] - row['dimensions']['z'] / 2.0
                       for row in model_sources)
    z_delta = target_floor - floor_before
    pios = _normalize_collect_pios(model_handles)
    container_type = (int(_safe(lambda: vs.GetTypeN(model_handles[0]), 0) or 0)
                      if len(model_handles) == 1 else 0)
    if _normalize_pio_group_move_risk(action, container_type, len(pios)):
        return {
            'error': ('editable Structural Members cannot be normalized into or moved as '
                      'an ordinary group; Vectorworks regenerates their paths/matrices and '
                      'the frame can snap away from the other geometry'),
            'required_container': 'editable symbol instance',
            'structural_member_count': len(pios),
            'action': action,
            'remediation': ('duplicate each direct member/object individually into a symbol '
                            'definition, verify after regeneration, then move the symbol '
                            'instance; do not duplicate or move the outer PIO group'),
        }
    for row in pios:
        row['target_matrix'] = [
            [row['matrix'][0][0], row['matrix'][0][1], row['matrix'][0][2] + z_delta],
            row['matrix'][1], row['matrix'][2], row['matrix'][3],
        ]
        row['target_center_z'] = row['center']['z'] + z_delta
    plan_id = 'assembly-%s' % uuid.uuid4().hex[:12]
    plan = {
        'plan_id': plan_id, 'confirm_token': uuid.uuid4().hex,
        'created_at': time.time(), 'status': 'planned', 'layer': layer_name,
        'assembly_name': assembly_name, 'helper_name': helper_name,
        'helper_class': helper_class, 'expected_model_count': expected_count,
        'target_floor': target_floor, 'floor_before': floor_before,
        'z_delta': z_delta, 'tolerance': tolerance, 'action': action,
        'model_sources': model_sources, 'helper_sources': helper_sources,
        'existing_helper_id': _oid(existing_helper) if existing_helper else None,
        'pios': pios, 'assembly_id': None, 'helper_id': None,
        'verification': None,
    }
    _ASSEMBLY_NORMALIZE_PLANS[plan_id] = plan
    if len(_ASSEMBLY_NORMALIZE_PLANS) > 20:
        completed = sorted((v for v in _ASSEMBLY_NORMALIZE_PLANS.values()
                            if v.get('status') in ('verified', 'verification_failed')),
                           key=lambda v: v.get('created_at', 0))
        for old in completed[:len(_ASSEMBLY_NORMALIZE_PLANS) - 20]:
            _ASSEMBLY_NORMALIZE_PLANS.pop(old['plan_id'], None)
    return {'status': 'planned', 'dry_run': True, 'plan_id': plan_id,
            'confirm_token': plan['confirm_token'], 'layer': layer_name,
            'assembly_name': assembly_name, 'helper_name': helper_name,
            'action': action, 'expected_model_count': expected_count,
            'helper_object_count': (len(helper_sources) + (1 if existing_helper else 0)),
            'structural_member_count': len(pios), 'floor_before': floor_before,
            'target_floor': target_floor, 'z_delta': z_delta,
            'next_action': 'execute_normalize_assembly with plan_id + confirm_token'}


def execute_normalize_assembly(p):
    """Execute/finalize a normalization plan in two post-regeneration calls."""
    plan = _ASSEMBLY_NORMALIZE_PLANS.get(str(p.get('plan_id') or ''))
    if not plan: return {'error': 'plan not found or expired after VW restart'}
    if str(p.get('confirm_token') or '') != plan['confirm_token']:
        return {'error': 'confirm_token does not match plan'}
    if plan['status'] == 'normalized_pending_pio_restore':
        errors = []
        for row in plan['pios']:
            h = _h(row['object_id'])
            matrix = row['target_matrix']
            if not h or not _safe(lambda h=h, matrix=matrix: vs.SetEntityMatrix(
                    h, tuple(matrix[0]), matrix[1], matrix[2], matrix[3]), False):
                errors.append({'name': row['name'], 'error': 'SetEntityMatrix failed'})
        assembly = _h(plan['assembly_id']) or _safe(lambda: vs.GetObject(plan['assembly_name']))
        helper = _h(plan['helper_id']) if plan.get('helper_id') else None
        if assembly: _safe(lambda: vs.ResetObject(assembly))
        if helper: _safe(lambda: vs.ResetObject(helper))
        _safe(lambda: vs.ReDrawAll())
        if errors:
            plan['status'] = 'pio_restore_failed'
            return {'error': 'Structural Member restore failed', 'details': errors,
                    'plan_id': plan['plan_id']}
        plan['status'] = 'normalized_pending_verification'
        return {'status': plan['status'], 'plan_id': plan['plan_id'],
                'restored_structural_members': len(plan['pios']),
                'next_action': 'verify_normalize_assembly in a new MCP call'}
    if plan['status'] != 'planned':
        return {'error': 'plan status must be planned or normalized_pending_pio_restore',
                'status': plan['status']}
    changed = _normalize_validate_plan_objects(plan)
    if changed: return {'error': 'objects changed since plan', 'details': changed}
    layer_h = _safe(lambda: vs.GetLayerByName(plan['layer']))
    if not layer_h: return {'error': 'target layer disappeared'}
    if bool(p.get('save_checkpoint', True)):
        _safe(lambda: vs.DoMenuTextByName('Save', 0))
    _safe(lambda: vs.NameUndoEvent('Agent: normalize assembly container and floor'))
    try:
        model_handles = [_h(row['object_id']) for row in plan['model_sources']]
        if plan['action'] == 'group':
            assembly = _normalize_new_group(layer_h, plan['assembly_name'])
            for h in model_handles: vs.SetParent(h, assembly)
        else:
            assembly = model_handles[0]
            if plan['action'] == 'rename_existing': vs.SetName(assembly, plan['assembly_name'])
        helper = _h(plan.get('existing_helper_id'))
        helper_handles = [_h(row['object_id']) for row in plan['helper_sources']]
        if helper_handles:
            if not helper: helper = _normalize_new_group(
                layer_h, plan['helper_name'], plan['helper_class'])
            for h in helper_handles: vs.SetParent(h, helper)
        if helper:
            vs.SetName(helper, plan['helper_name']); vs.SetClass(helper, plan['helper_class'])
        if abs(plan['z_delta']) > 1e-12:
            vs.Move3DObj(assembly, 0, 0, plan['z_delta'])
            if helper: vs.Move3DObj(helper, 0, 0, plan['z_delta'])
        plan['assembly_id'] = _oid(assembly)
        plan['helper_id'] = _oid(helper) if helper else None
        plan['status'] = 'normalized_pending_pio_restore'
        _safe(lambda: vs.ReDrawAll())
        return {'status': plan['status'], 'plan_id': plan['plan_id'],
                'assembly_id': plan['assembly_id'], 'helper_id': plan['helper_id'],
                'checkpoint_saved': bool(p.get('save_checkpoint', True)),
                'next_action': ('repeat execute_normalize_assembly with the same plan/token '
                                'in a new MCP call after deferred regeneration')}
    except Exception as exc:
        plan['status'] = 'execution_failed'
        return {'error': str(exc), 'status': plan['status'], 'plan_id': plan['plan_id'],
                'recovery': 'Use Vectorworks Undo or reopen the saved checkpoint'}


def _normalize_verify(plan):
    tolerance, mismatches = plan['tolerance'], []
    assembly = _h(plan.get('assembly_id')) or _safe(lambda: vs.GetObject(plan['assembly_name']))
    if not assembly:
        return {'clean': False, 'mismatches': [{'error': 'assembly missing'}]}
    layer_name = _safe(lambda: vs.GetLName(vs.GetLayer(assembly))) or ''
    if layer_name != plan['layer']:
        mismatches.append({'field': 'layer', 'expected': plan['layer'], 'actual': layer_name})
    if (_safe(lambda: vs.GetName(assembly)) or '') != plan['assembly_name']:
        mismatches.append({'field': 'assembly_name'})
    count = _normalize_semantic_count(assembly)
    if count != plan['expected_model_count']:
        mismatches.append({'field': 'model_count', 'expected': plan['expected_model_count'],
                           'actual': count})
    dims, _ = _dims3(assembly); center = _center3(assembly)
    floor = center['z'] - dims['z'] / 2.0 if dims and center else None
    if floor is None or abs(floor - plan['target_floor']) > tolerance:
        mismatches.append({'field': 'floor', 'expected': plan['target_floor'], 'actual': floor})
    helper = _h(plan.get('helper_id')) if plan.get('helper_id') else None
    if helper:
        if (_safe(lambda: vs.GetName(helper)) or '') != plan['helper_name']:
            mismatches.append({'field': 'helper_name'})
        if (_safe(lambda: vs.GetClass(helper)) or '') != plan['helper_class']:
            mismatches.append({'field': 'helper_class', 'expected': plan['helper_class'],
                               'actual': _safe(lambda: vs.GetClass(helper)) or ''})
    layer_h = _safe(lambda: vs.GetLayerByName(plan['layer']))
    allowed = set([_oid(assembly)] + ([_oid(helper)] if helper else []))
    strays, direct = [], _safe(lambda: vs.FInLayer(layer_h)) if layer_h else None
    while direct:
        if _oid(direct) not in allowed:
            strays.append({'object_id': _oid(direct),
                           'name': _safe(lambda direct=direct: vs.GetName(direct)) or '',
                           'type': int(_safe(lambda direct=direct: vs.GetTypeN(direct), 0) or 0)})
        direct = _safe(lambda direct=direct: vs.NextObj(direct))
    if strays:
        mismatches.append({'field': 'stray_top_level_objects', 'objects': strays})
    pio_errors = []
    for row in plan['pios']:
        h = _h(row['object_id']); matrix = _safe(lambda h=h: vs.GetEntityMatrix(h)) if h else None
        center_now = _center3(h) if h else None
        if not h or not matrix or not matrix[0] or not center_now:
            pio_errors.append({'name': row['name'], 'error': 'unreadable'})
            continue
        target = row['target_matrix'][0]
        if max(abs(float(matrix[1][i]) - float(target[i])) for i in range(3)) > tolerance:
            pio_errors.append({'name': row['name'], 'field': 'matrix.offset'})
        if abs(center_now['z'] - row['target_center_z']) > tolerance:
            pio_errors.append({'name': row['name'], 'field': 'center.z',
                               'expected': row['target_center_z'], 'actual': center_now['z']})
    mismatches.extend(pio_errors)
    return {'clean': not mismatches, 'assembly_id': _oid(assembly),
            'assembly_name': plan['assembly_name'], 'layer': layer_name,
            'expected_model_count': plan['expected_model_count'], 'actual_model_count': count,
            'target_floor': plan['target_floor'], 'actual_floor': floor,
            'structural_members_checked': len(plan['pios']), 'mismatches': mismatches}


def verify_normalize_assembly(p):
    plan = _ASSEMBLY_NORMALIZE_PLANS.get(str(p.get('plan_id') or ''))
    if not plan: return {'error': 'plan not found or expired after VW restart'}
    if plan['status'] not in ('normalized_pending_verification', 'verification_failed', 'verified'):
        return {'error': 'plan is not ready to verify', 'status': plan['status']}
    verification = _normalize_verify(plan)
    plan['verification'] = verification
    plan['status'] = 'verified' if verification['clean'] else 'verification_failed'
    return {'status': plan['status'], 'plan_id': plan['plan_id'],
            'verification': verification, 'final_save_required': verification['clean'],
            'recovery': (None if verification['clean'] else
                         'Use Vectorworks Undo or reopen the saved checkpoint')}


def assembly_floor_report(p):
    """Read-only common-floor QA for named top-level assembly containers."""
    names = p.get('assembly_names') or []
    if isinstance(names, str): names = [names]
    if not names: return {'error': 'assembly_names is required'}
    target = _float_or(p.get('target_floor'), 0.0)
    tolerance = abs(_float_or(p.get('tolerance'), 0.01))
    rows, errors = [], []
    for name in names:
        h = _safe(lambda name=name: vs.GetObject(str(name)))
        dims, _ = _dims3(h) if h else (None, None)
        center = _center3(h) if h and dims else None
        if not h or not dims or not center:
            errors.append({'name': name, 'error': 'assembly missing or unreadable'})
            continue
        floor = center['z'] - dims['z'] / 2.0
        rows.append({'name': str(name), 'object_id': _oid(h),
                     'type': int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0),
                     'layer': _safe(lambda h=h: vs.GetLName(vs.GetLayer(h))) or '',
                     'floor': floor, 'delta_from_target': floor - target,
                     'model_count': _normalize_semantic_count(h)})
    floors = [row['floor'] for row in rows]
    spread = (max(floors) - min(floors)) if floors else None
    clean = bool(rows) and len(rows) == len(names) and not errors and spread <= tolerance and all(
        abs(row['delta_from_target']) <= tolerance for row in rows)
    return {'status': 'ok', 'clean': clean, 'target_floor': target,
            'tolerance': tolerance, 'floor_spread': spread,
            'assemblies': rows, 'errors': errors}


def assembly_qa_bundle(p):
    """One-round-trip assembly inventory + floor QA.

    Params mirror get_assembly_manifest plus optional assembly_names,
    expected_manifest_count, target_floor, and tolerance. This is the fast
    default for agent QA after a batch edit: one file-IPC/menu-command trip
    instead of separate manifest, count, and floor-report calls.
    """
    manifest_params = {
        'layer': p.get('layer'),
        'region': p.get('region'),
        'roles': p.get('roles'),
        'profiles': p.get('profiles'),
        'name_prefix': p.get('name_prefix'),
        'limit': p.get('limit', 1000),
        'guard': p.get('guard', 60000),
    }
    manifest = get_assembly_manifest(manifest_params)
    if manifest.get('error'):
        return {'status': 'error', 'clean': False, 'manifest': manifest}
    names = p.get('assembly_names') or []
    floor = None
    if names:
        floor = assembly_floor_report({
            'assembly_names': names,
            'target_floor': p.get('target_floor', 0.0),
            'tolerance': p.get('tolerance', 0.01),
        })
    expected = p.get('expected_manifest_count')
    count_ok = expected is None or int(expected) == int(manifest.get('count', 0))
    floor_ok = floor is None or bool(floor.get('clean'))
    manifest_out = manifest
    if not bool(p.get('include_objects', False)):
        manifest_out = {k: v for k, v in manifest.items() if k != 'objects'}
        manifest_out['objects_omitted'] = len(manifest.get('objects') or [])
    return {
        'status': 'ok',
        'clean': bool(count_ok and floor_ok and not manifest.get('errors')),
        'manifest_count': int(manifest.get('count', 0)),
        'expected_manifest_count': expected,
        'count_ok': count_ok,
        'floor': floor,
        'manifest': manifest_out,
    }


def _normalize_native_box_specs(p):
    """Validate/normalize native-box batch input without touching Vectorworks."""
    items = p.get('items') or []
    if not isinstance(items, list) or not items:
        return None, 'items must be a non-empty list'
    if len(items) > int(p.get('max_items', 500)):
        return None, 'item count exceeds max_items'
    expected = p.get('expected_count')
    if expected is not None and int(expected) != len(items):
        return None, 'expected_count does not match items length'
    defaults = {
        'layer': p.get('layer'), 'class': p.get('class'),
        'material_name': p.get('material_name'),
        'texture_name': p.get('texture_name'),
        'parent_id': p.get('parent_id'),
    }
    rows, names = [], set()
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            return None, 'item %d must be an object' % index
        item = dict(defaults)
        item.update(raw)
        name = str(item.get('name') or '').strip()
        if not name:
            return None, 'item %d requires a unique name' % index
        if name in names:
            return None, 'duplicate item name: %s' % name
        names.add(name)
        center = item.get('center')
        dimensions = item.get('dimensions')
        try:
            if isinstance(center, dict) and isinstance(dimensions, dict):
                cx, cy, cz = (float(center[k]) for k in ('x', 'y', 'z'))
                width, depth, height = (float(dimensions[k]) for k in ('x', 'y', 'z'))
                x, y, z = cx - width / 2.0, cy - depth / 2.0, cz - height / 2.0
            else:
                x, y, z = (float(item[k]) for k in ('x', 'y', 'z'))
                width, depth, height = (float(item[k]) for k in ('width', 'depth', 'height'))
                cx, cy, cz = x + width / 2.0, y + depth / 2.0, z + height / 2.0
        except Exception:
            return None, ('item %d requires either center{x,y,z}+dimensions{x,y,z} '
                          'or x,y,z,width,depth,height' % index)
        if min(width, depth, height) <= 0:
            return None, 'item %d dimensions must be positive' % index
        item.update({'name': name, 'x': x, 'y': y, 'z': z,
                     'width': width, 'depth': depth, 'height': height,
                     'target_center': {'x': cx, 'y': cy, 'z': cz},
                     'target_dimensions': {'x': width, 'y': depth, 'z': height}})
        rows.append(item)
    return rows, None


def _native_box_reconcile_plan(desired, actual, tolerance=0.01, prune=False):
    """Pure desired-vs-live planner for named native extrudes.

    A matching name is the stable identity. Center/class changes are safe
    in-place updates; dimension changes require additive replacement because
    Vectorworks extrudes have no reliable generic resize operation.
    """
    by_name = {}
    duplicate_names = []
    for row in actual:
        name = str(row.get('name') or '')
        if not name:
            continue
        if name in by_name:
            duplicate_names.append(name)
        else:
            by_name[name] = row
    if duplicate_names:
        return None, 'ambiguous live object names: %s' % ', '.join(sorted(set(duplicate_names)))

    actions = {'create': [], 'update': [], 'replace': [], 'delete': [],
               'keep': [], 'unmanaged_extra': [], 'unsupported': []}
    desired_names = set()
    for target in desired:
        name = target['name']
        desired_names.add(name)
        live = by_name.get(name)
        if not live:
            actions['create'].append({'name': name, 'target': target})
            continue
        if int(live.get('type') or 0) != 24:
            actions['unsupported'].append({
                'name': name, 'object_id': live.get('object_id'),
                'reason': 'matched object is type %s, expected native extrude type 24' %
                          live.get('type'),
            })
            continue
        differences = []
        if not _values_equal(target['target_dimensions'], live.get('dimensions'), tolerance):
            differences.append('dimensions')
        if not _values_equal(target['target_center'], live.get('center'), tolerance):
            differences.append('center')
        if target.get('class') is not None and target.get('class') != live.get('class'):
            differences.append('class')
        if target.get('material_name') and target.get('material_name') != live.get('material_name'):
            differences.append('material_name')
        if target.get('texture_name') and target.get('texture_name') != live.get('texture_name'):
            differences.append('texture_name')
        if target.get('parent_id') is not None and target.get('parent_id') != live.get('parent_id'):
            differences.append('parent_id')
        item = {'name': name, 'object_id': live.get('object_id'),
                'target': target, 'actual': live, 'differences': differences}
        if 'dimensions' in differences or 'parent_id' in differences:
            actions['replace'].append(item)
        elif differences:
            actions['update'].append(item)
        else:
            actions['keep'].append(item)

    for live in actual:
        if live.get('name') in desired_names:
            continue
        if prune:
            if int(live.get('type') or 0) == 24:
                actions['delete'].append(live)
            else:
                actions['unsupported'].append({
                    'name': live.get('name'), 'object_id': live.get('object_id'),
                    'reason': 'refusing to prune non-extrude type %s' % live.get('type'),
                })
        else:
            actions['unmanaged_extra'].append(live)

    for rows in actions.values():
        rows.sort(key=lambda row: (str(row.get('name') or ''),
                                   str(row.get('object_id') or '')))
    summary = {key: len(value) for key, value in actions.items()}
    summary['converged'] = not any(actions[key] for key in
                                   ('create', 'update', 'replace', 'delete', 'unsupported'))
    summary['exact'] = summary['converged'] and not actions['unmanaged_extra']
    return {'actions': actions, 'summary': summary}, None


def _native_box_live_rows(layer, name_prefix, limit=1000, guard=60000):
    manifest = get_assembly_manifest({
        'layer': layer, 'name_prefix': name_prefix, 'limit': limit, 'guard': guard})
    if manifest.get('error'):
        return None, manifest.get('error')
    if manifest.get('truncated'):
        return None, 'live manifest was truncated; narrow the reconcile scope'
    if manifest.get('errors'):
        return None, 'live manifest contains unreadable objects: %s' % manifest['errors']
    rows = []
    for raw in manifest.get('objects') or []:
        row = dict(raw)
        h = _h(row.get('object_id'))
        if not h:
            return None, 'live object disappeared during inventory: %s' % row.get('object_id')
        mh = _safe(lambda h=h: vs.GetObjMaterialHandle(h))
        material_name = None
        if hasattr(vs, 'GetObjMaterialName'):
            raw_name = _safe(lambda h=h: vs.GetObjMaterialName(h))
            # VW2026 Python returns (success, name), despite older wrappers and
            # docs treating this as a bare string.
            if isinstance(raw_name, (tuple, list)) and len(raw_name) >= 2:
                material_name = str(raw_name[1]) if bool(raw_name[0]) else ''
            elif raw_name not in (None, ''):
                material_name = str(raw_name)
        if material_name is None:
            material_name = (_safe(lambda mh=mh: vs.GetName(mh)) or '') if mh else ''
        row['material_name'] = material_name
        ref = int(_safe(lambda h=h: vs.GetTextureRefN(h, 0, 0, True), 0) or 0)
        row['texture_name'] = _safe(lambda ref=ref: vs.Index2Name(ref), '') if ref else ''
        rows.append(row)
    return rows, None


def _selected_object_ids(limit=5000, guard=100000):
    """Return selected UUIDs across layers, groups, and viewport annotations."""
    selected, visited = [], [0]
    seen = set()

    def walk(h):
        while h:
            if visited[0] >= guard:
                raise RuntimeError('selection traversal exceeded guard')
            visited[0] += 1
            object_id = _oid(h)
            if object_id and bool(_safe(lambda h=h: vs.Selected(h), False)):
                if object_id not in seen:
                    selected.append(object_id); seen.add(object_id)
                    if len(selected) > limit:
                        raise RuntimeError('selection exceeds %d-object safety limit' % limit)
            object_type = int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0)
            if object_type == 11:
                child = _safe(lambda h=h: vs.FInGroup(h))
                if child: walk(child)
            if object_type in (89, 122):
                group = _safe(lambda h=h: vs.GetVPGroup(h, 2))
                child = _safe(lambda group=group: vs.FInGroup(group)) if group else None
                if child: walk(child)
            h = _safe(lambda h=h: vs.NextObj(h))

    try:
        for row in get_layers({}).get('layers') or []:
            if row.get('type') not in ('design', 'sheet'): continue
            layer = _safe(lambda row=row: vs.GetLayerByName(row['name']))
            if layer: walk(_safe(lambda layer=layer: vs.FInLayer(layer)))
    except Exception as exc:
        return None, str(exc)
    return sorted(selected), None


def _restore_selection_ids(selected_before, replacements=None, dropped=None):
    replacements = replacements or {}; dropped = set(dropped or [])
    try:
        current, error = _selected_object_ids()
        if error: return {'restored': False, 'error': error}
        for object_id in current:
            h = _h(object_id)
            if h: vs.SetDSelect(h)
        expected, missing = [], []
        for object_id in selected_before:
            if object_id in dropped: continue
            target_id = replacements.get(object_id, object_id)
            h = _h(target_id)
            if not h:
                missing.append(target_id); continue
            vs.SetSelect(h); expected.append(target_id)
        actual, error = _selected_object_ids()
        if error: return {'restored': False, 'error': error, 'missing_ids': missing}
        expected = sorted(set(expected))
        return {'restored': not missing and actual == expected,
                'expected_ids': expected, 'actual_ids': actual,
                'missing_ids': missing,
                'replacement_count': sum(1 for key in replacements if key in selected_before),
                'dropped_count': sum(1 for key in dropped if key in selected_before)}
    except Exception as exc:
        return {'restored': False, 'error': 'selection restoration failed: %s' % exc}


def _native_box_plan_token(layer, name_prefix, prune, desired, plan):
    payload = {
        'document': _safe(lambda: vs.GetFPathName()) or '',
        'layer': layer, 'name_prefix': name_prefix, 'prune': bool(prune),
        'desired': desired, 'actions': plan['actions'],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]


def _create_reconcile_box(row, object_name, tolerance):
    result = draw_box(row)
    if result.get('error') or not result.get('object_id'):
        raise RuntimeError(result.get('error') or 'draw_box returned no object_id')
    h = _h(result['object_id'])
    if not h:
        raise RuntimeError('created box could not be resolved')
    row_parent = _h(row.get('parent_id')) if row.get('parent_id') else None
    if row.get('parent_id') and not row_parent:
        raise RuntimeError('item parent_id not found: %s' % row.get('parent_id'))
    if row_parent and not bool(vs.SetParent(h, row_parent)):
        raise RuntimeError('SetParent failed for %s' % row['name'])
    if row.get('material_name'):
        material_result = assign_material({
            'object_id': _oid(h), 'material_name': row['material_name']})
        if material_result.get('error'):
            raise RuntimeError(material_result['error'])
    if row.get('texture_name'):
        texture_result = set_object_texture({
            'object_id': _oid(h), 'texture': row['texture_name']})
        if texture_result.get('error'):
            raise RuntimeError(texture_result['error'])
    if row.get('class') is not None:
        vs.SetClass(h, str(row['class']))
    vs.SetName(h, object_name)
    dims, _raw = _dims3(h)
    center = _center3(h)
    if not center or not dims:
        raise RuntimeError('created box readback failed for %s' % row['name'])
    if any(abs(center[k] - row['target_center'][k]) > tolerance for k in ('x', 'y', 'z')):
        raise RuntimeError('created box center mismatch for %s' % row['name'])
    if any(abs(dims[k] - row['target_dimensions'][k]) > tolerance for k in ('x', 'y', 'z')):
        raise RuntimeError('created box dimensions mismatch for %s' % row['name'])
    return h


def reconcile_verified_native_boxes(p):
    """Converge a safely scoped set of named native boxes on desired state.

    Dry-run returns an exact plan token. A mutation requires confirm=true and
    that token; the live document is re-read first, so user edits between plan
    and apply fail closed. Missing objects are created, center/style changes are
    updated, dimension changes are additively replaced, and scoped extras are
    deleted only when prune=true.
    """
    rows, error = _normalize_native_box_specs(p)
    if error:
        return {'error': error}
    layer = str(p.get('layer') or '').strip()
    name_prefix = str(p.get('name_prefix') or '').strip()
    if not layer or not name_prefix:
        return {'error': 'layer and name_prefix are required safety scopes'}
    if any(not row['name'].startswith(name_prefix) for row in rows):
        return {'error': 'every desired name must start with name_prefix'}
    tolerance = abs(_float_or(p.get('tolerance'), 0.01))
    prune = bool(p.get('prune', False))
    if not _safe(lambda: vs.GetLayerByName(layer)):
        return {'error': 'target layer not found: %s' % layer}
    for row in rows:
        if row.get('layer') and str(row['layer']) != layer:
            return {'error': 'item layer must match reconcile layer: %s' % row['name']}
        row['layer'] = layer
        if row.get('material_name') and not _safe(lambda row=row: vs.GetObject(str(row['material_name']))):
            return {'error': 'material not found: %s' % row['material_name']}
        if row.get('texture_name') and not _safe(lambda row=row: vs.Name2Index(str(row['texture_name'])), 0):
            return {'error': 'texture not found: %s' % row['texture_name']}

    actual, error = _native_box_live_rows(
        layer, name_prefix, p.get('limit', 1000), p.get('guard', 60000))
    if error:
        return {'error': error}
    plan, error = _native_box_reconcile_plan(rows, actual, tolerance, prune)
    if error:
        return {'error': error}
    for action in plan['actions']['create']:
        collision = _safe(lambda action=action: vs.GetObject(action['name']))
        if collision:
            return {'error': 'desired create name is already used outside the reconcile scope: %s' %
                    action['name']}
    token = _native_box_plan_token(layer, name_prefix, prune, rows, plan)
    base = {'status': 'dry_run', 'plan_token': token, 'layer': layer,
            'name_prefix': name_prefix, 'prune': prune, **plan}
    if not bool(p.get('confirm', False)):
        return base
    if str(p.get('plan_token') or '') != token:
        return {'error': 'plan_token missing or stale; run a fresh dry-run',
                'current_plan_token': token, **plan}
    if plan['actions']['unsupported']:
        return {'error': 'reconcile plan contains unsupported live objects', **base}
    if plan['summary']['converged']:
        return {'status': 'ok', 'changed': False, 'plan_token': token, **plan}

    selected_before, selection_error = _selected_object_ids()
    if selection_error:
        return {'error': 'could not snapshot selection: %s' % selection_error, **base}

    created = []
    moved = []
    classed = []
    styled = []
    replacements = []
    selection_replacements = {}
    selection_dropped = set()
    destructive_started = False
    try:
        # Additive work first. Existing geometry is not deleted until every new
        # or updated object has passed exact readback.
        for action in plan['actions']['create']:
            h = _create_reconcile_box(action['target'], action['name'], tolerance)
            created.append(h)
        for action in plan['actions']['replace']:
            temp_name = '%s__VWX-RECONCILE-%s' % (name_prefix, uuid.uuid4().hex[:12])
            h = _create_reconcile_box(action['target'], temp_name, tolerance)
            created.append(h)
            replacements.append((action, h))
        for action in plan['actions']['update']:
            h = _h(action['object_id'])
            if not h or (_safe(lambda h=h: vs.GetName(h)) or '') != action['name']:
                raise RuntimeError('live object changed before update: %s' % action['name'])
            if 'center' in action['differences']:
                original = _center3(h)
                target = action['target']['target_center']
                vs.Move3DObj(h, target['x'] - original['x'], target['y'] - original['y'],
                             target['z'] - original['z'])
                moved.append((h, original))
            if 'class' in action['differences']:
                original_class = _safe(lambda h=h: vs.GetClass(h)) or ''
                vs.SetClass(h, str(action['target']['class']))
                classed.append((h, original_class))
            if ('material_name' in action['differences'] or
                    'texture_name' in action['differences']):
                styled.append((
                    h,
                    _safe(lambda h=h: vs.GetObjMaterialHandle(h)),
                    int(_safe(lambda h=h: vs.GetTextureRefN(h, 0, 0, False), 0) or 0),
                ))
            if 'material_name' in action['differences']:
                result = assign_material({'object_id': _oid(h),
                                          'material_name': action['target']['material_name']})
                if result.get('error'):
                    raise RuntimeError(result['error'])
            if 'texture_name' in action['differences']:
                result = set_object_texture({'object_id': _oid(h),
                                             'texture': action['target']['texture_name']})
                if result.get('error'):
                    raise RuntimeError(result['error'])
            center = _center3(h)
            dims, _raw = _dims3(h)
            target = action['target']
            if (not center or not dims or
                    not _values_equal(target['target_center'], center, tolerance) or
                    not _values_equal(target['target_dimensions'], dims, tolerance)):
                raise RuntimeError('update readback failed for %s' % action['name'])

        # Resolve every destructive target before deleting the first one. Once
        # deletion starts, recovery is the single Vectorworks Undo event; an
        # in-process rollback cannot recreate arbitrary deleted objects.
        replacement_sources = []
        for action, new_h in replacements:
            old_h = _h(action['object_id'])
            if not old_h:
                raise RuntimeError('replacement source disappeared: %s' % action['name'])
            replacement_sources.append((action, old_h, new_h))
        prune_targets = []
        for action in plan['actions']['delete']:
            h = _h(action.get('object_id'))
            if not h:
                raise RuntimeError('prune target disappeared: %s' % action.get('name'))
            prune_targets.append((action, h))

        # Destructive commit is deliberately last.
        destructive_started = True
        for action, old_h, new_h in replacement_sources:
            selection_replacements[action['object_id']] = _oid(new_h)
            vs.DelObject(old_h)
            vs.SetName(new_h, action['name'])
        for action, h in prune_targets:
            selection_dropped.add(action['object_id'])
            vs.DelObject(h)

        final_actual, error = _native_box_live_rows(
            layer, name_prefix, p.get('limit', 1000), p.get('guard', 60000))
        if error:
            raise RuntimeError(error)
        final_plan, error = _native_box_reconcile_plan(rows, final_actual, tolerance, prune)
        if error or not final_plan['summary']['converged']:
            raise RuntimeError(error or 'post-reconcile verification did not converge')
        selection = _restore_selection_ids(
            selected_before, selection_replacements, selection_dropped)
        if not selection.get('restored'):
            return {'status': 'partial', 'changed': True, 'plan_token': token,
                    'error': 'geometry converged but prior selection could not be restored',
                    'selection': selection, 'verification': final_plan,
                    'recovery': 'Restore selection manually; geometry is verified and converged.'}
        return {'status': 'ok', 'changed': True, 'plan_token': token,
                'applied': {key: len(plan['actions'][key]) for key in
                            ('create', 'update', 'replace', 'delete')},
                'verification': final_plan, 'selection': selection}
    except Exception as exc:
        if not destructive_started:
            for h, material_h, texture_ref in reversed(styled):
                _safe(lambda h=h, material_h=material_h: vs.SetObjMaterialHandle(h, material_h))
                _safe(lambda h=h, texture_ref=texture_ref: vs.SetTextureRefN(
                    h, texture_ref, 0, 0))
            for h, original_class in reversed(classed):
                _safe(lambda h=h, original_class=original_class: vs.SetClass(h, original_class))
            for h, original in reversed(moved):
                current = _center3(h)
                if current:
                    _safe(lambda h=h, current=current, original=original: vs.Move3DObj(
                        h, original['x'] - current['x'], original['y'] - current['y'],
                        original['z'] - current['z']))
            for h in reversed(created):
                _safe(lambda h=h: vs.DelObject(h))
        selection = _restore_selection_ids(
            selected_before, selection_replacements, selection_dropped)
        return {'error': str(exc), 'destructive_started': destructive_started,
                'rollback_attempted': not destructive_started,
                'selection': selection,
                'recovery': ('Use Vectorworks Undo for the single reconcile event.'
                             if destructive_started else None)}


def create_verified_native_boxes(p):
    """Create, style, parent, and verify many native boxes in one round-trip.

    Additive and fail-closed: preflights every item/resource/name first; requires
    confirm=true to mutate; rolls back every object created by this call if any
    creation/readback/count/floor check fails. Each item accepts either corner
    x/y/z + width/depth/height or center{x,y,z} + dimensions{x,y,z}. Global
    defaults: layer, class, material_name, texture_name, parent_id.
    """
    rows, error = _normalize_native_box_specs(p)
    if error:
        return {'error': error}
    tolerance = abs(_float_or(p.get('tolerance'), 0.01))
    parent = _h(p.get('parent_id')) if p.get('parent_id') else None
    if p.get('parent_id') and not parent:
        return {'error': 'parent_id not found'}
    # Full preflight before any mutation.
    for row in rows:
        if _safe(lambda row=row: vs.GetObject(row['name'])):
            return {'error': 'object name already exists: %s' % row['name']}
        if row.get('layer') and not _safe(lambda row=row: vs.GetLayerByName(str(row['layer']))):
            return {'error': 'target layer not found: %s' % row['layer']}
        if row.get('material_name') and not _safe(lambda row=row: vs.GetObject(str(row['material_name']))):
            return {'error': 'material not found: %s' % row['material_name']}
        if row.get('texture_name') and not _safe(lambda row=row: vs.Name2Index(str(row['texture_name'])), 0):
            return {'error': 'texture not found: %s' % row['texture_name']}
    if not bool(p.get('confirm', False)):
        return {'status': 'dry_run', 'count': len(rows), 'items': rows,
                'note': 'Pass confirm=true to create the verified native boxes.'}
    before_count = _normalize_semantic_count(parent) if parent else None
    floor_before = None
    if p.get('assembly_names'):
        floor_before = assembly_floor_report({
            'assembly_names': p.get('assembly_names'),
            'target_floor': p.get('target_floor', 0.0),
            'tolerance': tolerance,
        })
    created = []
    readback = []
    try:
        for row in rows:
            result = draw_box(row)
            if result.get('error') or not result.get('object_id'):
                raise RuntimeError(result.get('error') or 'draw_box returned no object_id')
            h = _h(result['object_id'])
            if not h:
                raise RuntimeError('created box could not be resolved')
            created.append(h)
            row_parent = _h(row.get('parent_id')) if row.get('parent_id') else parent
            if row.get('parent_id') and not row_parent:
                raise RuntimeError('item parent_id not found: %s' % row.get('parent_id'))
            if row_parent and not bool(vs.SetParent(h, row_parent)):
                raise RuntimeError('SetParent failed for %s' % row['name'])
            if row.get('material_name'):
                material_result = assign_material({
                    'object_id': _oid(h), 'material_name': row['material_name']})
                if material_result.get('error'):
                    raise RuntimeError(material_result['error'])
            if row.get('texture_name'):
                texture_result = set_object_texture({
                    'object_id': _oid(h), 'texture': row['texture_name']})
                if texture_result.get('error'):
                    raise RuntimeError(texture_result['error'])
            if row.get('class') is not None:
                vs.SetClass(h, str(row['class']))
            vs.SetName(h, row['name'])  # last: reparenting can clear names on extrudes
            dims, _raw = _dims3(h)
            center = _center3(h)
            actual_parent = _safe(lambda h=h: vs.GetParent(h))
            expected_parent = row_parent
            checks = {
                'name': (_safe(lambda h=h: vs.GetName(h)) or '') == row['name'],
                'center': bool(center) and all(
                    abs(center[k] - row['target_center'][k]) <= tolerance
                    for k in ('x', 'y', 'z')),
                'dimensions': bool(dims) and all(
                    abs(dims[k] - row['target_dimensions'][k]) <= tolerance
                    for k in ('x', 'y', 'z')),
                'parent': expected_parent is None or actual_parent == expected_parent,
            }
            if not all(checks.values()):
                raise RuntimeError('readback verification failed for %s: %s' %
                                   (row['name'], checks))
            readback.append({'object_id': _oid(h), 'name': row['name'],
                             'center': center, 'dimensions': dims,
                             'checks': checks})
        after_count = _normalize_semantic_count(parent) if parent else None
        expected_after = p.get('expected_model_count_after')
        if expected_after is not None and int(expected_after) != int(after_count):
            raise RuntimeError('model count mismatch: expected %s, got %s' %
                               (expected_after, after_count))
        if before_count is not None and after_count != before_count + len(rows):
            raise RuntimeError('parent model count did not increase by created count')
        floor_after = None
        if p.get('assembly_names'):
            floor_after = assembly_floor_report({
                'assembly_names': p.get('assembly_names'),
                'target_floor': p.get('target_floor', 0.0),
                'tolerance': tolerance,
            })
            if bool(p.get('require_clean_floor', False)) and not floor_after.get('clean'):
                raise RuntimeError('post-create floor verification failed')
        return {'status': 'ok', 'created_count': len(created),
                'created': readback, 'parent_id': _oid(parent) if parent else None,
                'model_count_before': before_count, 'model_count_after': after_count,
                'floor_before': floor_before, 'floor_after': floor_after}
    except Exception as exc:
        for h in reversed(created):
            _safe(lambda h=h: vs.DelObject(h))
        return {'error': str(exc), 'rolled_back': len(created),
                'created_count': 0}


def _normalize_bulk_transform_specs(p):
    """Validate bulk world-center moves without touching Vectorworks."""
    items = p.get('items') or []
    if not isinstance(items, list) or not items:
        return None, 'items must be a non-empty list'
    if len(items) > int(p.get('max_items', 500)):
        return None, 'item count exceeds max_items'
    expected = p.get('expected_count')
    if expected is not None and int(expected) != len(items):
        return None, 'expected_count does not match items length'
    rows, seen = [], set()
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            return None, 'item %d must be an object' % index
        object_id = str(raw.get('object_id') or '').strip()
        if not object_id:
            return None, 'item %d requires object_id' % index
        if object_id in seen:
            return None, 'duplicate object_id: %s' % object_id
        seen.add(object_id)
        target = raw.get('target_center')
        delta = raw.get('delta')
        if target is not None and delta is not None:
            return None, 'item %d cannot set both target_center and delta' % index
        if target is None and delta is None:
            return None, 'item %d requires target_center or delta' % index
        if target is not None and not isinstance(target, dict):
            return None, 'item %d target_center must be an object' % index
        if delta is not None and not isinstance(delta, dict):
            return None, 'item %d delta must be an object' % index
        source = target if target is not None else delta
        if not any(k in source for k in ('x', 'y', 'z')):
            return None, 'item %d transform needs at least one x/y/z value' % index
        normalized = {'object_id': object_id,
                      'mode': 'target_center' if target is not None else 'delta'}
        try:
            normalized[normalized['mode']] = {
                k: float(source[k]) for k in ('x', 'y', 'z') if k in source}
        except Exception:
            return None, 'item %d transform values must be numeric' % index
        rows.append(normalized)
    return rows, None


def bulk_transform_objects(p):
    """Move many safe 3D leaf objects by world center with rollback/readback.

    Symbols, extrudes, solids, sweeps, spheres, meshes, and NURBS are accepted by
    default. Groups and PIOs are rejected because their regeneration/container
    behavior needs dedicated workflows. Dry-run until confirm=true.
    """
    specs, error = _normalize_bulk_transform_specs(p)
    if error:
        return {'error': error}
    tolerance = abs(_float_or(p.get('tolerance'), 0.01))
    default_types = [15, 24, 25, 26, 28, 40, 84, 91]
    allowed_types = set(int(v) for v in (p.get('allowed_types') or default_types))
    prepared = []
    for spec in specs:
        h = _h(spec['object_id'])
        if not h:
            return {'error': 'object_id not found: %s' % spec['object_id']}
        object_type = int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0)
        if object_type not in allowed_types:
            return {'error': 'object type %d is not allowed for bulk transform' % object_type,
                    'object_id': spec['object_id'], 'allowed_types': sorted(allowed_types)}
        center = _center3(h)
        dims, _raw = _dims3(h)
        if not center or not dims:
            return {'error': 'could not read object center/dimensions',
                    'object_id': spec['object_id']}
        if spec['mode'] == 'target_center':
            values = spec['target_center']
            target = {k: values.get(k, center[k]) for k in ('x', 'y', 'z')}
        else:
            values = spec['delta']
            target = {k: center[k] + values.get(k, 0.0) for k in ('x', 'y', 'z')}
        prepared.append({
            'object_id': spec['object_id'], 'handle': h, 'type': object_type,
            'name': _safe(lambda h=h: vs.GetName(h)) or '',
            'parent': _safe(lambda h=h: vs.GetParent(h)),
            'dimensions': dims,
            'original_center': center, 'target_center': target,
            'delta': {k: target[k] - center[k] for k in ('x', 'y', 'z')},
            'target_bottom_z': target['z'] - dims['z'] / 2.0,
        })
    minimum_z = p.get('minimum_z')
    if minimum_z is None and bool(p.get('require_clean_floor', False)):
        minimum_z = p.get('target_floor', 0.0)
    if minimum_z is not None:
        minimum_z = float(minimum_z)
        violations = [
            {'object_id': row['object_id'], 'target_bottom_z': row['target_bottom_z']}
            for row in prepared if row['target_bottom_z'] < minimum_z - tolerance]
        if violations:
            return {'error': 'target geometry would fall below minimum_z',
                    'minimum_z': minimum_z, 'violations': violations,
                    'mutation_performed': False}
    plan_rows = [{k: v for k, v in row.items() if k not in ('handle', 'parent')}
                 for row in prepared]
    if not bool(p.get('confirm', False)):
        return {'status': 'dry_run', 'count': len(plan_rows), 'items': plan_rows,
                'allowed_types': sorted(allowed_types),
                'note': 'Pass confirm=true to apply the verified bulk transform.'}
    floor_before = None
    if p.get('assembly_names'):
        floor_before = assembly_floor_report({
            'assembly_names': p.get('assembly_names'),
            'target_floor': p.get('target_floor', 0.0),
            'tolerance': tolerance,
        })
    moved = []
    try:
        for row in prepared:
            d = row['delta']
            vs.Move3DObj(row['handle'], d['x'], d['y'], d['z'])
            actual = _center3(row['handle'])
            actual_dims, _raw = _dims3(row['handle'])
            checks = {
                'center': bool(actual) and all(
                    abs(actual[k] - row['target_center'][k]) <= tolerance
                    for k in ('x', 'y', 'z')),
                'name': (_safe(lambda row=row: vs.GetName(row['handle'])) or '') == row['name'],
                'parent': _safe(lambda row=row: vs.GetParent(row['handle'])) == row['parent'],
                'type': int(_safe(lambda row=row: vs.GetTypeN(row['handle']), 0) or 0) == row['type'],
                'dimensions': bool(actual_dims) and all(
                    abs(actual_dims[k] - row['dimensions'][k]) <= tolerance
                    for k in ('x', 'y', 'z')),
            }
            moved.append(row)
            if not all(checks.values()):
                raise RuntimeError('transform readback failed for %s: %s' %
                                   (row['object_id'], checks))
            row['actual_center'] = actual
            row['actual_bottom_z'] = actual['z'] - actual_dims['z'] / 2.0
            row['checks'] = checks
        floor_after = None
        if p.get('assembly_names'):
            floor_after = assembly_floor_report({
                'assembly_names': p.get('assembly_names'),
                'target_floor': p.get('target_floor', 0.0),
                'tolerance': tolerance,
            })
            if bool(p.get('require_clean_floor', False)) and not floor_after.get('clean'):
                raise RuntimeError('post-transform floor verification failed')
        result_rows = [{k: v for k, v in row.items() if k not in ('handle', 'parent')}
                       for row in prepared]
        return {'status': 'ok', 'moved_count': len(moved), 'items': result_rows,
                'floor_before': floor_before, 'floor_after': floor_after}
    except Exception as exc:
        rollback_errors = []
        for row in reversed(moved):
            current = _center3(row['handle'])
            if not current:
                rollback_errors.append({'object_id': row['object_id'],
                                        'error': 'center unreadable during rollback'})
                continue
            vs.Move3DObj(row['handle'],
                         row['original_center']['x'] - current['x'],
                         row['original_center']['y'] - current['y'],
                         row['original_center']['z'] - current['z'])
            restored = _center3(row['handle'])
            if not restored or any(abs(restored[k] - row['original_center'][k]) > tolerance
                                   for k in ('x', 'y', 'z')):
                rollback_errors.append({'object_id': row['object_id'],
                                        'error': 'rollback readback mismatch'})
        return {'error': str(exc), 'rolled_back': len(moved) - len(rollback_errors),
                'rollback_clean': not rollback_errors,
                'rollback_errors': rollback_errors}


def _face_value(v):
    if isinstance(v, dict): v = v.get('value')
    return _float_or(v, None)


def place_member_between_faces(p):
    """Duplicate an axis-aligned Structural Member and fit it between two faces.

    Params: profile_source_id, axis ('x'|'y'), face_a, face_b, transverse,
    elevation, optional side_offset/name/layer/class/tolerance. Elevation is the
    requested member-center Z. The source must already be
    parallel to `axis`; this avoids unreliable live-document rotation tricks.
    Equal StartOffset/EndOffset changes preserve the member center and are
    verified with Get3DInfo before the new member is kept.
    """
    src = _h(p.get('profile_source_id'))
    if not src: return {'error': 'profile_source_id not found'}
    axis = str(p.get('axis') or '').lower()
    if axis not in ('x', 'y'):
        return {'error': "axis must be 'x' or 'y'"}
    tolerance = abs(_float_or(p.get('tolerance'), 0.01))
    fa, fb = _face_value(p.get('face_a')), _face_value(p.get('face_b'))
    if fa is None or fb is None or abs(fb - fa) <= tolerance:
        return {'error': 'face_a and face_b must be distinct numeric coordinates'}
    transverse = _float_or(p.get('transverse'), None)
    elevation = _float_or(p.get('elevation'), None)
    if transverse is None or elevation is None:
        return {'error': 'transverse and elevation are required'}
    side_offset = _float_or(p.get('side_offset'), 0.0)
    rec_name = _pio_record(src)
    profile = (_safe(lambda: vs.GetRField(src, rec_name, 'ProfileSymbol')) or '') if rec_name else ''
    if not rec_name or not profile:
        return {'error': 'source is not a Structural Member with ProfileSymbol'}
    dims, _ = _dims3(src)
    center = _center3(src)
    if not dims or not center:
        return {'error': 'could not read source dimensions/center'}
    source_axis = _primary_axis(dims)
    if source_axis != axis:
        return {'error': 'source axis is %s; choose a source parallel to %s' %
                         (source_axis, axis), 'source_axis': source_axis}
    desired = abs(fb - fa)
    current = dims[axis]
    old_start = _float_or(_safe(lambda: vs.GetRField(src, rec_name, 'StartOffset')), 0.0)
    old_end = _float_or(_safe(lambda: vs.GetRField(src, rec_name, 'EndOffset')), 0.0)
    target_layer = None
    if p.get('layer'):
        target_layer = _safe(lambda: vs.GetLayerByName(str(p['layer'])))
        if not target_layer:
            return {'error': 'target layer not found: %s' % p['layer']}
    target_class = p.get('class') if p.get('class') is not None else p.get('obj_class')
    nh = _safe(lambda: vs.HDuplicate(src, 0, 0))
    if not nh: return {'error': 'HDuplicate failed'}
    try:
        delta = desired - current
        vs.SetRField(nh, rec_name, 'StartOffset', str(old_start + delta / 2.0))
        vs.SetRField(nh, rec_name, 'EndOffset', str(old_end + delta / 2.0))
        vs.ResetObject(nh)
        if target_layer: vs.SetParent(nh, target_layer)
        if target_class is not None: vs.SetClass(nh, str(target_class))
        mid = (fa + fb) / 2.0
        target = ({'x': mid, 'y': transverse + side_offset, 'z': elevation}
                  if axis == 'x' else
                  {'x': transverse + side_offset, 'y': mid, 'z': elevation})
        c = _center3(nh)
        if not c:
            raise RuntimeError('could not read duplicate center after resize')
        vs.Move3DObj(nh, target['x'] - c['x'], target['y'] - c['y'],
                     target['z'] - c['z'])
        if p.get('name'): vs.SetName(nh, str(p['name']))
        vs.ResetObject(nh)
        actual_dims, raw = _dims3(nh)
        actual_center = _center3(nh)
        if not actual_dims or not actual_center:
            raise RuntimeError('could not verify duplicate dimensions/center')
        length_ok = abs(actual_dims[axis] - desired) <= tolerance
        center_ok = all(abs(actual_center[k] - target[k]) <= tolerance
                        for k in ('x', 'y', 'z'))
        if not length_ok or not center_ok:
            vs.DelObject(nh)
            return {'error': 'placement verification failed; duplicate removed',
                    'target': target, 'actual_center': actual_center,
                    'desired_length': desired, 'actual_dimensions': actual_dims}
        return {
            'status': 'ok', 'object_id': _oid(nh), 'source_id': _oid(src),
            'name': _safe(lambda: vs.GetName(nh)) or '', 'profile': profile,
            'axis': axis, 'faces': [fa, fb], 'target_center': target,
            'source_dimensions': dims,
            'center': actual_center, 'dimensions': actual_dims,
            'dimensions_raw_yxz': raw,
            'layer': _safe(lambda: vs.GetLName(vs.GetLayer(nh))) or '',
            'class': _safe(lambda: vs.GetClass(nh)) or '',
            'parent_id': _oid(_safe(lambda: vs.GetParent(nh))),
            'material': _safe(lambda: vs.GetRField(src, rec_name, 'MemberMaterial')) or '',
            'texture': _safe(lambda: vs.GetRField(src, rec_name, 'MemberTexture_3D')) or '',
            'start_offset': _safe(lambda: vs.GetRField(nh, rec_name, 'StartOffset')),
            'end_offset': _safe(lambda: vs.GetRField(nh, rec_name, 'EndOffset')),
        }
    except Exception as exc:
        _safe(lambda: vs.DelObject(nh))
        return {'error': str(exc), 'duplicate_removed': True}


def native_member_fallback_spec(p):
    """Read-only native-box spec derived from one Structural Member source."""
    src = _h(p.get('profile_source_id') or p.get('source_id'))
    if not src: return {'error': 'profile_source_id/source_id not found'}
    axis = str(p.get('axis') or '').lower()
    if axis not in ('x', 'y'): return {'error': "axis must be 'x' or 'y'"}
    fa, fb = _face_value(p.get('face_a')), _face_value(p.get('face_b'))
    transverse = _float_or(p.get('transverse'), None)
    elevation = _float_or(p.get('elevation'), None)
    if fa is None or fb is None or transverse is None or elevation is None:
        return {'error': 'face_a/face_b/transverse/elevation are required'}
    rec_name = _pio_record(src)
    profile = (_safe(lambda: vs.GetRField(src, rec_name, 'ProfileSymbol')) or '') if rec_name else ''
    dims, _raw = _dims3(src)
    if not rec_name or not profile or not dims:
        return {'error': 'source is not a readable Structural Member'}
    desired = abs(fb - fa)
    target_dims = dict(dims); target_dims[axis] = desired
    side_offset = _float_or(p.get('side_offset'), 0.0)
    mid = (fa + fb) / 2.0
    target = ({'x': mid, 'y': transverse + side_offset, 'z': elevation}
              if axis == 'x' else
              {'x': transverse + side_offset, 'y': mid, 'z': elevation})
    parent = _safe(lambda: vs.GetParent(src))
    layer_name = str(p.get('layer') or '')
    if layer_name:
        layer_h = _safe(lambda: vs.GetLayerByName(layer_name))
        if not layer_h: return {'error': 'target layer not found: %s' % layer_name}
        parent = layer_h
    else:
        layer_name = _safe(lambda: vs.GetLName(vs.GetLayer(src))) or ''
    target_class = p.get('class') if p.get('class') is not None else p.get('obj_class')
    if target_class is None: target_class = _safe(lambda: vs.GetClass(src)) or ''
    return {'status': 'ok', 'source_id': _oid(src), 'profile': profile,
            'axis': axis, 'target_center': target, 'dimensions': target_dims,
            'desired_length': desired, 'name': str(p.get('name') or ''),
            'layer': layer_name, 'class': target_class,
            'parent_id': _oid(parent),
            'material': _safe(lambda: vs.GetRField(src, rec_name, 'MemberMaterial')) or '',
            'texture': _safe(lambda: vs.GetRField(src, rec_name, 'MemberTexture_3D')) or ''}


def _finite_center(center):
    if not isinstance(center, dict): return False
    try:
        return all(math.isfinite(float(center[k])) and abs(float(center[k])) < 1e12
                   for k in ('x', 'y', 'z'))
    except Exception:
        return False


def _pio_child_integrity_signature(h, limit=64):
    """Immediate generated-child type + finite-center signature for a PIO.

    Duplicated Structural Members can retain correct outer dimensions while one
    generated child group becomes invalid (VW sentinel center near 3.9e98). A
    source-vs-duplicate signature mismatch catches that false-green state.
    """
    rows = []
    child = _safe(lambda: vs.FInGroup(h))
    while child and len(rows) < int(limit):
        center = _center3(child)
        rows.append({'type': int(_safe(lambda child=child: vs.GetTypeN(child), 0) or 0),
                     'finite_center': _finite_center(center)})
        child = _safe(lambda child=child: vs.NextObj(child))
    return rows


def finalize_member_between_faces(p):
    """Post-regeneration matrix persistence for a provisional Structural Member."""
    h = _h(p.get('object_id'))
    src = _h(p.get('source_id'))
    if not h or not src: return {'error': 'object_id/source_id not found'}
    axis = str(p.get('axis') or '').lower()
    if axis not in ('x', 'y'): return {'error': "axis must be 'x' or 'y'"}
    target = p.get('target_center') or {}
    try: target = {k: float(target[k]) for k in ('x', 'y', 'z')}
    except Exception: return {'error': 'target_center requires numeric x/y/z'}
    tolerance = abs(_float_or(p.get('tolerance'), 0.01))
    center = _center3(h)
    matrix = _safe(lambda: vs.GetEntityMatrix(h))
    if not _finite_center(center) or not isinstance(matrix, (tuple, list)) or len(matrix) < 5 or not matrix[0]:
        return {'error': 'could not read provisional center/entity matrix'}
    offset = matrix[1]
    try:
        new_offset = tuple(float(offset[i]) + target[k] - center[k]
                           for i, k in enumerate(('x', 'y', 'z')))
    except Exception:
        return {'error': 'invalid entity matrix offset'}
    if not bool(_safe(lambda: vs.SetEntityMatrix(
            h, new_offset, float(matrix[2]), float(matrix[3]), float(matrix[4])), False)):
        return {'error': 'SetEntityMatrix failed'}
    actual_center = _center3(h)
    actual_dims, raw = _dims3(h)
    desired = abs(_float_or(p.get('desired_length'), 0.0))
    checks = {
        'center': _finite_center(actual_center) and all(
            abs(actual_center[k] - target[k]) <= tolerance for k in ('x', 'y', 'z')),
        'length': bool(actual_dims) and desired > 0 and
                  abs(actual_dims[axis] - desired) <= tolerance,
        'type': int(_safe(lambda: vs.GetTypeN(h), 0) or 0) == 86,
    }
    if not all(checks.values()):
        return {'error': 'post-regeneration matrix readback failed',
                'checks': checks, 'center': actual_center,
                'dimensions': actual_dims}
    return {'status': 'finalized_pending_fresh_verification',
            'object_id': _oid(h), 'source_id': _oid(src), 'axis': axis,
            'target_center': target, 'desired_length': desired,
            'center': actual_center, 'dimensions': actual_dims,
            'dimensions_raw_yxz': raw, 'checks': checks,
            'matrix_offset': list(new_offset)}


def verify_member_between_faces_fresh(p):
    """Fresh-dispatch dimensions/matrix/generated-child integrity verification."""
    h = _h(p.get('object_id'))
    src = _h(p.get('source_id'))
    if not h or not src: return {'error': 'object_id/source_id not found', 'clean': False}
    axis = str(p.get('axis') or '').lower()
    target = p.get('target_center') or {}
    try: target = {k: float(target[k]) for k in ('x', 'y', 'z')}
    except Exception: return {'error': 'target_center requires numeric x/y/z', 'clean': False}
    desired = abs(_float_or(p.get('desired_length'), 0.0))
    tolerance = abs(_float_or(p.get('tolerance'), 0.01))
    center = _center3(h); dims, raw = _dims3(h)
    matrix = _safe(lambda: vs.GetEntityMatrix(h))
    source_sig = _pio_child_integrity_signature(src)
    object_sig = _pio_child_integrity_signature(h)
    expected_parent_id = str(p.get('parent_id') or '')
    actual_parent_id = _oid(_safe(lambda: vs.GetParent(h))) or ''
    checks = {
        'type': int(_safe(lambda: vs.GetTypeN(h), 0) or 0) == 86,
        'center': _finite_center(center) and all(
            abs(center[k] - target[k]) <= tolerance for k in ('x', 'y', 'z')),
        'length': bool(dims) and desired > 0 and abs(dims.get(axis, 0) - desired) <= tolerance,
        'matrix': isinstance(matrix, (tuple, list)) and len(matrix) >= 5 and bool(matrix[0]),
        'parent': not expected_parent_id or actual_parent_id == expected_parent_id,
        'generated_children': bool(source_sig) and object_sig == source_sig,
    }
    clean = all(checks.values())
    return {'status': 'verified' if clean else 'verification_failed',
            'clean': clean, 'object_id': _oid(h), 'source_id': _oid(src),
            'center': center, 'dimensions': dims, 'dimensions_raw_yxz': raw,
            'checks': checks, 'parent_id': actual_parent_id,
            'source_child_signature': source_sig,
            'object_child_signature': object_sig}


def verify_native_member_fallback(p):
    """Fresh verification for one native-box member fallback."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object_id not found', 'clean': False}
    target = p.get('target_center') or {}; expected_dims = p.get('dimensions') or {}
    try:
        target = {k: float(target[k]) for k in ('x', 'y', 'z')}
        expected_dims = {k: float(expected_dims[k]) for k in ('x', 'y', 'z')}
    except Exception:
        return {'error': 'target_center/dimensions require numeric x/y/z', 'clean': False}
    tolerance = abs(_float_or(p.get('tolerance'), 0.01))
    center = _center3(h); dims, raw = _dims3(h)
    expected_parent_id = str(p.get('parent_id') or '')
    actual_parent_id = _oid(_safe(lambda: vs.GetParent(h))) or ''
    expected_name = str(p.get('name') or '')
    checks = {
        'type': int(_safe(lambda: vs.GetTypeN(h), 0) or 0) == 24,
        'center': _finite_center(center) and all(
            abs(center[k] - target[k]) <= tolerance for k in ('x', 'y', 'z')),
        'dimensions': bool(dims) and all(
            abs(dims[k] - expected_dims[k]) <= tolerance for k in ('x', 'y', 'z')),
        'parent': not expected_parent_id or actual_parent_id == expected_parent_id,
        'name': not expected_name or (_safe(lambda: vs.GetName(h)) or '') == expected_name,
    }
    clean = all(checks.values())
    return {'status': 'verified' if clean else 'verification_failed',
            'clean': clean, 'object_id': _oid(h), 'center': center,
            'dimensions': dims, 'dimensions_raw_yxz': raw,
            'parent_id': actual_parent_id, 'checks': checks}


def save_qa_view(p):
    """Save the current view + layer/class visibility as a named QA Saved View.
    Existing views with the same name are replaced without opening a dialog."""
    name = str(p.get('name') or '').strip()
    if not name: return {'error': 'name is required'}
    if bool(p.get('replace', True)):
        _safe(lambda: vs.VDelete(name))
    vs.SaveSheet(name, bool(p.get('save_view', True)), bool(p.get('save_classes', True)),
                  bool(p.get('save_layers', True)))
    return {'status': 'ok', 'name': name,
            'saved_view': bool(p.get('save_view', True)),
            'saved_classes': bool(p.get('save_classes', True)),
            'saved_layers': bool(p.get('save_layers', True))}


def restore_qa_view(p):
    """Restore a named Vectorworks Saved View."""
    name = str(p.get('name') or '').strip()
    if not name: return {'error': 'name is required'}
    vs.VRestore(name)
    return {'status': 'ok', 'name': name}


# ── SDK enrichment 2: architecture, lights, criteria, worksheets, text, edit ─

def create_roof(p):
    """Create a roof from a footprint. params: {edges:[{x,y,slope,projection,
    eave_height},...], gable(bool), bearing_inset, thickness, miter_type(1..4),
    vert_miter}. Edges in order around the footprint; slope in deg."""
    edges = p.get('edges') or []
    if len(edges) < 3: return {'error': 'need >=3 edges [{x,y,slope,projection,eave_height}]'}
    roof = vs.CreateRoof(bool(p.get('gable', False)), float(p.get('bearing_inset', 0)),
                         float(p.get('thickness', 200)), int(p.get('miter_type', 1)),
                         float(p.get('vert_miter', 0)))
    if not roof: return {'error': 'CreateRoof failed'}
    for e in edges:
        vs.AppendRoofEdge(roof, (float(e.get('x', 0)), float(e.get('y', 0))),
                          float(e.get('slope', 30)), float(e.get('projection', 300)),
                          float(e.get('eave_height', 2500)))
    return {'status': 'ok', 'object_id': _oid(roof), 'edges': len(edges)}

def create_slab(p):
    """Create a slab from a closed 2D profile. params: {object_id} (profile consumed)."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'profile not found'}
    s = vs.CreateSlab(h)
    if not s: return {'error': 'CreateSlab failed (profile must be closed 2D)'}
    return {'status': 'ok', 'object_id': _oid(s)}

def join_walls(p):
    """Join two walls (T or L join). params: {wall_id_a, wall_id_b, mode(1=T,2=L),
    capped(bool)}."""
    a = _h(p.get('wall_id_a')); b = _h(p.get('wall_id_b'))
    if not a or not b: return {'error': 'wall_id_a and wall_id_b required'}
    ok = vs.JoinWalls(a, b, a, b, int(p.get('mode', 2)), bool(p.get('capped', True)), False)
    return {'status': 'ok', 'joined': bool(ok)}

def add_symbol_to_wall(p):
    """Insert a symbol (door/window) into a wall. params: {wall_id, symbol_name,
    offset (along wall), height, flip(bool), right(bool)}."""
    w = _h(p.get('wall_id'))
    if not w: return {'error': 'wall not found'}
    if not p.get('symbol_name'): return {'error': 'symbol_name required'}
    vs.AddSymToWall(w, float(p.get('offset', 0)), float(p.get('height', 0)),
                    bool(p.get('flip', False)), bool(p.get('right', False)),
                    p['symbol_name'])
    return {'status': 'ok', 'object_id': _oid(vs.LNewObj())}

def set_wall_style(p):
    """Apply a wall style by name. params: {object_id, style}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'wall not found'}
    ok = vs.SetWallStyle(h, p.get('style', ''), 0, 0)
    return {'status': 'ok', 'applied': bool(ok)}

def get_wall_style(p):
    """Wall style name of a wall. params: {object_id}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'wall not found'}
    return {'status': 'ok', 'style': vs.GetWallStyle(h)}

def create_light(p):
    """Create a light. params: {x,y,z, light_type(1=directional,2=point,3=spot),
    on(bool), shadows(bool), brightness(0-100)}."""
    lt = int(p.get('light_type', 2))
    h = vs.CreateLight(float(p.get('x', 0)), float(p.get('y', 0)), float(p.get('z', 1000)),
                       lt, bool(p.get('on', True)), bool(p.get('shadows', True)))
    if not h: return {'error': 'CreateLight failed'}
    if p.get('brightness') is not None:
        vs.SetLightInfo(h, lt, int(p['brightness']), bool(p.get('on', True)),
                        bool(p.get('shadows', True)))
    return {'status': 'ok', 'object_id': _oid(h)}

def set_light_info(p):
    """Set light attributes. params: {object_id, light_type, brightness(0-100),
    on(bool), shadows(bool)}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'light not found'}
    vs.SetLightInfo(h, int(p.get('light_type', 2)), int(p.get('brightness', 75)),
                    bool(p.get('on', True)), bool(p.get('shadows', True)))
    return {'status': 'ok'}

def get_light_info(p):
    """Light attributes. params: {object_id}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'light not found'}
    r = vs.GetLightInfo(h)
    try:
        return {'status': 'ok', 'light_type': r[0], 'brightness': r[1],
                'on': bool(r[2]), 'shadows': bool(r[3])}
    except Exception:
        return {'status': 'ok', 'info': r}

def criteria_count(p):
    """Count objects matching a VW criteria string, e.g. "(T=RECT)" or
    "(L='Layer-1')" or "((R IN ['Baumkataster']))". params: {criteria}."""
    c = p.get('criteria')
    if not c: return {'error': 'criteria required'}
    return {'status': 'ok', 'count': vs.Count(c)}

def select_by_criteria(p):
    """Select all objects matching a criteria string. params: {criteria}.
    Returns how many are selected afterwards."""
    c = p.get('criteria')
    if not c: return {'error': 'criteria required'}
    vs.SelectObj(c)
    return {'status': 'ok', 'selected': vs.NumSelectedObjects()}

def deselect_by_criteria(p):
    """Deselect all objects matching a criteria string. params: {criteria}."""
    c = p.get('criteria')
    if not c: return {'error': 'criteria required'}
    vs.DSelectObj(c)
    return {'status': 'ok', 'selected': vs.NumSelectedObjects()}

def eval_expression(p):
    """Evaluate a worksheet-style expression on ONE object, e.g. 'AREA',
    'PERIM', a record field "('Rec'.'Field')". params: {object_id, expression,
    as_string(bool)}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    expr = p.get('expression')
    if not expr: return {'error': 'expression required'}
    if p.get('as_string'):
        return {'status': 'ok', 'value': vs.EvalStr(h, expr)}
    return {'status': 'ok', 'value': vs.Eval(h, expr)}

def _ws(p):
    name = p.get('worksheet') or p.get('name')
    h = vs.GetObject(name) if name else None
    return h

def get_worksheet_cell(p):
    """Displayed string + numeric value of one worksheet cell.
    params: {worksheet(name), row, column} (1-based)."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    r, c = int(p.get('row', 1)), int(p.get('column', 1))
    return {'status': 'ok', 'text': vs.GetWSCellString(ws, r, c),
            'value': _safe(lambda: vs.GetWSCellValue(ws, r, c))}

def get_worksheet_size(p):
    """(rows, columns) of a worksheet. params: {worksheet(name)}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    r = vs.GetWSRowColumnCount(ws)
    try:
        return {'status': 'ok', 'rows': r[0], 'columns': r[1]}
    except Exception:
        return {'status': 'ok', 'size': r}

def insert_worksheet_rows(p):
    """Insert rows. params: {worksheet(name), before_row, count}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    vs.InsertWSRows(ws, int(p.get('before_row', 1)), int(p.get('count', 1)))
    return {'status': 'ok'}

def delete_worksheet_rows(p):
    """Delete rows. params: {worksheet(name), start_row, count}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    vs.DeleteWSRows(ws, int(p.get('start_row', 1)), int(p.get('count', 1)))
    return {'status': 'ok'}

def insert_worksheet_columns(p):
    """Insert columns. params: {worksheet(name), before_column, count}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    vs.InsertWSColumns(ws, int(p.get('before_column', 1)), int(p.get('count', 1)))
    return {'status': 'ok'}

def set_worksheet_column_width(p):
    """Set column width(s). params: {worksheet(name), from_column, to_column, width}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    fc = int(p.get('from_column', 1))
    vs.SetWSColumnWidth(ws, fc, int(p.get('to_column', fc)), int(p.get('width', 80)))
    return {'status': 'ok'}

def get_text(p):
    """Content of a text object. params: {object_id}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'text object not found'}
    return {'status': 'ok', 'text': vs.GetText(h)}

def set_text(p):
    """Replace content of a text object. params: {object_id, text}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'text object not found'}
    vs.SetText(h, p.get('text', ''))
    return {'status': 'ok'}

def set_text_size_all(p):
    """Set font size of the WHOLE text object. params: {object_id, size(pt)}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'text object not found'}
    n = vs.GetTextLength(h)
    vs.SetTextSize(h, 0, n, float(p.get('size', 12)))
    return {'status': 'ok', 'chars': n}

def convert_to_polygon(p):
    """Convert any 2D object to a polygon (arcs tessellated). params:
    {object_id, resolution (segments per arc, default 32)}. Original untouched."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    r = vs.ConvertToPolygon(h, int(p.get('resolution', 32)))
    return {'status': 'ok', 'object_id': _oid(r)}

def convert_to_polyline(p):
    """Convert a 2D object to a polyline (arcs preserved as arc vertices).
    params: {object_id}. Original untouched."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    r = vs.MakePolyline(h)
    return {'status': 'ok', 'object_id': _oid(r)}

def set_stacking_order(p):
    """Move object in the drawing stacking order. params: {object_id,
    action: 'front'|'forward'|'backward'|'back'}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    a = p.get('action', 'front')
    if a in ('front', 'forward'): vs.HMoveForward(h, a == 'front')
    elif a in ('back', 'backward'): vs.HMoveBackward(h, a == 'back')
    else: return {'error': "action must be front|forward|backward|back"}
    return {'status': 'ok'}

def move_object_3d(p):
    """Move an object in 3D by a delta. params: {object_id, dx, dy, dz}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    vs.Move3DObj(h, float(p.get('dx', 0)), float(p.get('dy', 0)), float(p.get('dz', 0)))
    return {'status': 'ok', 'object_id': p.get('object_id')}

def create_shell(p):
    """Thicken a NURBS surface into a shelled solid. params: {object_id, thickness}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'NURBS surface not found'}
    s = vs.CreateShell(h, float(p.get('thickness', 10)))
    if not s: return {'error': 'CreateShell failed (input must be a NURBS surface)'}
    return {'status': 'ok', 'object_id': _oid(s)}

def revolve_with_rail(p):
    """Revolve a profile around an axis, optionally following a rail curve.
    params: {profile_id, rail_id, axis_id} (axis = a line defining the axis)."""
    prof = _h(p.get('profile_id')); rail = _h(p.get('rail_id')); axis = _h(p.get('axis_id'))
    if not prof or not axis: return {'error': 'profile_id and axis_id required'}
    s = vs.RevolveWithRail(prof, rail if rail else prof, axis)
    if not s: return {'error': 'RevolveWithRail produced nothing (check geometry planes)'}
    return {'status': 'ok', 'object_id': _oid(s)}

def offset_nurbs(p):
    """Offset a NURBS curve/surface by a distance. params: {object_id, distance}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'NURBS object not found'}
    r = vs.CreateOffsetNurbsObjectHandle(h, float(p.get('distance', 10)))
    if not r: return {'error': 'offset failed (not a NURBS object?)'}
    return {'status': 'ok', 'object_id': _oid(r)}

def extend_nurbs_curve(p):
    """Extend a NURBS curve at start or end. params: {object_id, distance,
    at_start(bool), linear(bool)}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'NURBS curve not found'}
    r = vs.ExtendNurbsCurve(h, float(p.get('distance', 50)),
                            bool(p.get('at_start', False)), bool(p.get('linear', True)))
    return {'status': 'ok', 'object_id': _oid(r if r else h)}

def set_layer_elevation(p):
    """Set base elevation (Z) + thickness (deltaZ) of a design layer.
    params: {layer(name), elevation, thickness}."""
    h = vs.GetLayerByName(p.get('layer', ''))
    if not h: return {'error': 'layer not found'}
    vs.SetLayerElevation(h, float(p.get('elevation', 0)), float(p.get('thickness', 0)))
    return {'status': 'ok'}

def get_layer_elevation(p):
    """Base elevation + thickness of a design layer. params: {layer(name)}."""
    h = vs.GetLayerByName(p.get('layer', ''))
    if not h: return {'error': 'layer not found'}
    r = vs.GetLayerElevation(h)
    try:
        return {'status': 'ok', 'elevation': r[0], 'thickness': r[1]}
    except Exception:
        return {'status': 'ok', 'raw': r}

def set_view_angles(p):
    """Set the 3D view by rotation angles + offset (like the flyover tool).
    params: {x_angle, y_angle, z_angle, dx, dy, dz} (deg / doc units)."""
    vs.SetView(float(p.get('x_angle', -60)), float(p.get('y_angle', 0)),
               float(p.get('z_angle', -15)), float(p.get('dx', 0)),
               float(p.get('dy', 0)), float(p.get('dz', 0)))
    return {'status': 'ok'}

def get_object_metrics(p):
    """Area + perimeter of a 2D object (doc units). params: {object_id}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    return {'status': 'ok', 'area': _safe(lambda: vs.HArea(h)),
            'perimeter': _safe(lambda: vs.HPerim(h))}

def get_document_units(p):
    """Current document unit settings (raw GetUnits tuple: fraction, display,
    format, upi, unit name, square unit name)."""
    r = vs.GetUnits()
    try:
        return {'status': 'ok', 'fraction': r[0], 'display': r[1], 'format': r[2],
                'units_per_inch': r[3], 'name': r[4], 'square_name': r[5]}
    except Exception:
        return {'status': 'ok', 'raw': r}

def set_projection(p):
    """Fail closed: VW2026's Python Projection callback is not script-safe."""
    return {
        'error': ('set_projection is disabled: every tested VW2026 Python '
                  'Projection signature opens Script Errors and may block the bridge'),
        'remediation': ('use set_3d_view for orientation and change render mode '
                        'through a verified menu command/UI path'),
    }

# ── SDK enrichment 4: GIS coordinate engine + polygon vertex editing ────────

def _nums(r):
    """Strip leading ok-flags from a VW return tuple; keep the numbers.
    (True, 51.3, 9.4) -> [51.3, 9.4]; scalars pass through as [scalar]."""
    if isinstance(r, (list, tuple)):
        vals = [v for v in r if isinstance(v, (int, float)) and not isinstance(v, bool)]
        return vals
    return [r] if isinstance(r, (int, float)) else []

def geo_to_drawing(p):
    """Convert WGS84 lat/lon to drawing coordinates using the document's
    georeferencing. params: {lat, lon}. Requires a georeferenced document."""
    r = vs.GeogCoordToVWN(float(p.get('lat', 0)), float(p.get('lon', 0)))
    pts = _pts_from(r if isinstance(r, (list, tuple)) else (r,))
    if pts: return {'status': 'ok', 'x': pts[0][0], 'y': pts[0][1]}
    v = _nums(r)
    if len(v) >= 2: return {'status': 'ok', 'x': v[0], 'y': v[1]}
    return {'status': 'ok', 'raw': r}

def drawing_to_geo(p):
    """Convert drawing coordinates to WGS84 lat/lon using the document's
    georeferencing. params: {x, y}."""
    r = vs.VWCoordToGeogN(float(p.get('x', 0)), float(p.get('y', 0)))
    v = _nums(r)
    if len(v) >= 2: return {'status': 'ok', 'lat': v[0], 'lon': v[1]}
    pts = _pts_from(r if isinstance(r, (list, tuple)) else (r,))
    if pts: return {'status': 'ok', 'lat': pts[0][0], 'lon': pts[0][1]}
    return {'status': 'ok', 'raw': r}

def get_georeference_info(p):
    """Georeferencing summary: origin (lat/lon), angle to north, projection of
    the active (or named) layer. params: {layer?}."""
    lh = vs.GetLayerByName(p['layer']) if p.get('layer') else vs.ActLayer()
    origin = _nums(_safe(lambda: vs.GetGISOriginN()))
    proj = _safe(lambda: vs.GetProjectionLocName(lh))
    if isinstance(proj, (list, tuple)): proj = proj[-1]
    return {'status': 'ok',
            'origin_lat': (origin[0] if len(origin) >= 2 else None),
            'origin_lon': (origin[1] if len(origin) >= 2 else None),
            'angle_to_north': _safe(lambda: vs.GetAngleToNorth()),
            'is_georeferenced': bool(_safe(lambda: vs.IsGeoreferenced(lh))),
            'projection': proj,
            'project_elevation': _safe(lambda: vs.GetProjectElevation())}

def get_projection(p):
    """Projection of a layer as WKT + Proj4. params: {layer?, esri_style(bool)}."""
    lh = vs.GetLayerByName(p['layer']) if p.get('layer') else vs.ActLayer()
    esri = bool(p.get('esri_style', False))
    def _sv(r):
        if isinstance(r, (list, tuple)): return r[-1]
        return r
    return {'status': 'ok',
            'name': _sv(_safe(lambda: vs.GetProjectionLocName(lh))),
            'wkt': _sv(_safe(lambda: vs.GetProjectionWKT(lh, esri))),
            'proj4': _sv(_safe(lambda: vs.GetProjectionProj4(lh, esri)))}

def set_document_georef(p):
    """Georeference the document by EPSG code using the current user origin.
    params: {epsg} (e.g. 25832 for ETRS89/UTM32N)."""
    epsg = int(p.get('epsg', 0))
    if not epsg: return {'error': 'epsg required (e.g. 25832)'}
    vs.SetDocGeoRefByUsrOrg(epsg)
    return {'status': 'ok', 'epsg': epsg}

def get_poly_vertices(p):
    """All vertices of a polygon/polyline as [[x,y],...]. params: {object_id}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    n = vs.GetVertNum(h)
    verts = []
    for i in range(1, int(n) + 1):
        pt = vs.GetPolyPt(h, i)
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            verts.append([pt[0], pt[1]])
    return {'status': 'ok', 'count': int(n), 'vertices': verts,
            'closed': bool(_safe(lambda: vs.IsPolyClosed(h)))}

def set_poly_vertex(p):
    """Move one vertex. params: {object_id, index (1-based), x, y}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    vs.SetPolyPt(h, int(p.get('index', 1)), float(p.get('x', 0)), float(p.get('y', 0)))
    return {'status': 'ok'}

def insert_poly_vertex(p):
    """Insert a vertex. params: {object_id, x, y, before (1-based),
    vertex_type (0=corner 1=bezier 2=cubic 3=arc), radius}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    vs.InsertVertex(h, float(p.get('x', 0)), float(p.get('y', 0)),
                    int(p.get('before', 1)), int(p.get('vertex_type', 0)),
                    float(p.get('radius', 0)))
    return {'status': 'ok', 'count': int(vs.GetVertNum(h))}

def delete_poly_vertex(p):
    """Delete one vertex. params: {object_id, index (1-based)}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    vs.DelVertex(h, int(p.get('index', 1)))
    return {'status': 'ok', 'count': int(vs.GetVertNum(h))}

def set_poly_closed(p):
    """Open/close a polygon/polyline. params: {object_id, closed(bool)}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    vs.SetPolyClosed(h, bool(p.get('closed', True)))
    return {'status': 'ok', 'closed': bool(vs.IsPolyClosed(h))}

def get_poly_holes(p):
    """Openings (holes) of a polyline. params: {object_id}. Returns hole ids."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    n = _safe(lambda: vs.GetNumHoles(h))
    if isinstance(n, (list, tuple)): n = n[-1] if n else 0
    n = int(n or 0)
    holes = []
    for i in range(1, n + 1):
        hh = _safe(lambda i=i: vs.GetHole(h, i))
        if isinstance(hh, (list, tuple)): hh = hh[-1]
        if hh: holes.append(_oid(hh))
    return {'status': 'ok', 'count': n, 'holes': holes}


# ── SDK enrichment 3: report worksheets, IFC deep, textures, doc defaults ───

def _col_letter(i):
    """1 -> A, 2 -> B, ... 27 -> AA (worksheet column letters)."""
    s = ''
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s

def create_report_worksheet(p):
    """One-call criteria-driven report (database worksheet): creates the
    worksheet, writes header row, binds row 2 to a DATABASE(criteria) and fills
    the column formulas, recalculates, optionally places it on the drawing.
    params: {name, criteria, columns:[{header, formula}], place_at:{x,y}?}
    Formula examples: '=N' (object name), '=AREA', '=PERIM',
    "='Rec'.'Field'" (record field), '=C' (class), '=L' (layer), '=COUNT'.
    Returns worksheet name, subrow count (matching objects) + image id if placed."""
    name = p.get('name') or 'Report'
    crit = p.get('criteria')
    cols = p.get('columns') or []
    if not crit: return {'error': 'criteria required, e.g. "(T=RECT)" or "((R IN [\'Baumkataster\']))"'}
    if not cols: return {'error': "columns required: [{header, formula}, ...]"}
    old = vs.GetObject(name)
    if old: return {'error': "worksheet '%s' already exists — pick another name" % name}
    ws = vs.CreateWS(name, 2, max(1, len(cols)))
    if not ws: return {'error': 'CreateWS failed'}
    for i, c in enumerate(cols, start=1):
        vs.SetWSCellFormula(ws, 1, i, 1, i, str(c.get('header', 'Col%d' % i)))
    # row 2 col 0 = the database row binding
    vs.SetWSCellFormula(ws, 2, 0, 2, 0, '=DATABASE(%s)' % crit)
    for i, c in enumerate(cols, start=1):
        f = str(c.get('formula', ''))
        if f: vs.SetWSCellFormula(ws, 2, i, 2, i, f)
    vs.RecalculateWS(ws)
    sub = _safe(lambda: vs.GetWSSubrowCount(ws, 2))
    out = {'status': 'ok', 'worksheet': name, 'columns': len(cols),
           'subrows': (sub if isinstance(sub, int) else (sub[0] if sub else 0))}
    pa = p.get('place_at')
    if pa:
        img = vs.CreateWSImage(ws, (float(pa.get('x', 0)), float(pa.get('y', 0))))
        out['image_id'] = _oid(img)
    return out

def set_worksheet_database_row(p):
    """Bind a worksheet row to a criteria database. params: {worksheet(name),
    row, criteria}. Row then auto-populates one subrow per matching object."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    crit = p.get('criteria')
    if not crit: return {'error': 'criteria required'}
    r = int(p.get('row', 2))
    vs.SetWSCellFormula(ws, r, 0, r, 0, '=DATABASE(%s)' % crit)
    vs.RecalculateWS(ws)
    sub = _safe(lambda: vs.GetWSSubrowCount(ws, r))
    return {'status': 'ok', 'subrows': (sub if isinstance(sub, int) else (sub[0] if sub else 0))}

def get_worksheet_subrow_count(p):
    """Subrow count of a database row. params: {worksheet(name), row}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    r = int(p.get('row', 2))
    if not _safe(lambda: vs.IsWSDatabaseRow(ws, r)):
        return {'error': 'row %d is not a database row' % r}
    sub = vs.GetWSSubrowCount(ws, r)
    return {'status': 'ok', 'subrows': (sub if isinstance(sub, int) else (sub[0] if sub else 0))}

def get_worksheet_subrow_cell(p):
    """Read one database SUBROW cell (string + numeric).
    params: {worksheet(name), row (database row), subrow (1-based), column}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    r, sr, c = int(p.get('row', 2)), int(p.get('subrow', 1)), int(p.get('column', 1))
    return {'status': 'ok',
            'text': vs.GetWSSubrowCellString(ws, r, c, sr),
            'value': _safe(lambda: vs.GetWSSubrowCellValue(ws, r, c, sr))}

def get_worksheet_cell_formula(p):
    """Formula stored in a cell. params: {worksheet(name), row, column}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    return {'status': 'ok', 'formula': vs.GetWSCellFormula(ws, int(p.get('row', 1)), int(p.get('column', 1)))}

def set_worksheet_cell_alignment(p):
    """Horizontal alignment of cell range. params: {worksheet(name), row, column,
    to_row?, to_column?, alignment (1=general 2=left 3=right 4=center)}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    r, c = int(p.get('row', 1)), int(p.get('column', 1))
    vs.SetWSCellAlignment(ws, r, c, int(p.get('to_row', r)), int(p.get('to_column', c)),
                          int(p.get('alignment', 4)))
    return {'status': 'ok'}

def set_worksheet_cell_text_format(p):
    """Font/size/style of cell range. params: {worksheet(name), row, column,
    to_row?, to_column?, font(name)?, size(pt), style (0=plain 1=bold 2=italic ...)}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    r, c = int(p.get('row', 1)), int(p.get('column', 1))
    fid = vs.GetFontID(p['font']) if p.get('font') else -1
    vs.SetWSCellTextFormat(ws, r, c, int(p.get('to_row', r)), int(p.get('to_column', c)),
                           fid, int(p.get('size', 10)), int(p.get('style', 0)))
    return {'status': 'ok'}

def set_worksheet_cell_number_format(p):
    """Number format of cell range. params: {worksheet(name), row, column,
    to_row?, to_column?, style (0=general 1=decimal 2=scientific 3=fractional
    4=dimension 5=angle ...), accuracy (decimals), leader?, trailer?}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    r, c = int(p.get('row', 1)), int(p.get('column', 1))
    vs.SetWSCellNumberFormat(ws, r, c, int(p.get('to_row', r)), int(p.get('to_column', c)),
                             int(p.get('style', 1)), int(p.get('accuracy', 2)),
                             str(p.get('leader', '')), str(p.get('trailer', '')))
    return {'status': 'ok'}

def set_worksheet_cell_fill(p):
    """Cell background fill. params: {worksheet(name), row, column, to_row?,
    to_column?, style(0=none 1=solid), bg_color(index), fg_color(index), pattern}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    r, c = int(p.get('row', 1)), int(p.get('column', 1))
    vs.SetWSCellFill(ws, r, c, int(p.get('to_row', r)), int(p.get('to_column', c)),
                     int(p.get('style', 1)), int(p.get('bg_color', 0)),
                     int(p.get('fg_color', 255)), int(p.get('pattern', 1)))
    return {'status': 'ok'}

def set_worksheet_row_height(p):
    """Row height. params: {worksheet(name), from_row, to_row?, height, lock(bool)}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    fr = int(p.get('from_row', 1))
    vs.SetWSRowHeight(ws, fr, int(p.get('to_row', fr)), int(p.get('height', 18)),
                      True, bool(p.get('lock', False)))
    return {'status': 'ok'}

def merge_worksheet_cells(p):
    """Merge a cell range into one cell. params: {worksheet(name), row, column,
    to_row, to_column}."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    ok = vs.WorksheetMergeCells(ws, int(p.get('row', 1)), int(p.get('column', 1)),
                                int(p.get('to_row', 1)), int(p.get('to_column', 2)))
    return {'status': 'ok', 'merged': bool(ok)}

def place_worksheet_on_drawing(p):
    """Place (or find) the worksheet's on-drawing image. params: {worksheet(name),
    x, y}. Returns the image object id."""
    ws = _ws(p)
    if not ws: return {'error': 'worksheet not found (by name)'}
    existing = _safe(lambda: vs.GetWSImage(ws))
    if existing: return {'status': 'ok', 'object_id': _oid(existing), 'existed': True}
    img = vs.CreateWSImage(ws, (float(p.get('x', 0)), float(p.get('y', 0))))
    return {'status': 'ok', 'object_id': _oid(img), 'existed': False}

# ---- IFC deep ----------------------------------------------------------------

def ifc_list_psets(p):
    """List property sets attached to an object (name + property count).
    params: {object_id, all(bool: include inherited/type psets)}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    ball = bool(p.get('all', True))
    n = _safe(lambda: vs.IFC_GetNumPsets2(h, ball))
    if isinstance(n, (list, tuple)): n = n[-1] if n else 0
    n = int(n or 0)
    psets = []
    for i in range(n):
        info = _safe(lambda i=i: vs.IFC_GetPsetInfoAt(h, ball, i))
        # VW2026: (ok, name, flags) tuple — keep only real entries
        if isinstance(info, (list, tuple)) and len(info) >= 2 and info[0] and info[1]:
            psets.append(info[1])
        elif isinstance(info, str) and info:
            psets.append(info)
    return {'status': 'ok', 'count': len(psets), 'psets': psets}

def ifc_get_pset_prop(p):
    """Read one pset property value. params: {object_id, pset, prop}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    r = _safe(lambda: vs.IFC_GetPsetProp(h, p.get('pset', ''), p.get('prop', '')))
    # VW2026: (ok, value, ifcTypeCode) — value is index 1, NOT last
    if isinstance(r, (list, tuple)) and len(r) >= 2:
        return {'status': 'ok', 'found': bool(r[0]), 'value': r[1],
                'ifc_type': (r[2] if len(r) > 2 else None)}
    return {'status': 'ok', 'value': r}

def ifc_attach_pset(p):
    """Attach an (existing/defined) pset to an object. params: {object_id, pset}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    ok = vs.IFC_AttachPset(h, p.get('pset', ''))
    return {'status': 'ok', 'attached': bool(ok)}

def ifc_remove_pset(p):
    """Remove one pset (or ALL) from an object. params: {object_id, pset?} —
    omit pset (or pass all:true) to clear everything."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    if p.get('pset') and not p.get('all'):
        ok = vs.IFC_ClearPset(h, p['pset'])
    else:
        ok = vs.IFC_ClearAllPsets(h)
    return {'status': 'ok', 'removed': bool(ok)}

def ifc_define_pset(p):
    """Define a custom pset schema (document-wide). params: {name,
    members:[{name, type}]} — type is an IFC type string like 'IfcLabel',
    'IfcReal', 'IfcBoolean', 'IfcLengthMeasure'."""
    name = p.get('name')
    members = p.get('members') or []
    if not name or not members: return {'error': 'name + members required'}
    if not vs.IFC_DefPsetBegin(name): return {'error': 'DefPsetBegin failed'}
    added = 0
    for m in members:
        if vs.IFC_DefPsetAddMember(name, m.get('name', ''), m.get('type', 'IfcLabel')):
            added += 1
    ok = vs.IFC_DefPsetEnd(name)
    return {'status': 'ok', 'defined': bool(ok), 'members_added': added}

def ifc_get_entity_prop(p):
    """Read a direct IFC entity attribute (Name, Description, Tag, ...).
    params: {object_id, prop}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    r = _safe(lambda: vs.IFC_GetEntityProp(h, p.get('prop', '')))
    # VW2026: (ok, value, ifcTypeCode) — value is index 1
    if isinstance(r, (list, tuple)) and len(r) >= 2:
        return {'status': 'ok', 'found': bool(r[0]), 'value': r[1]}
    return {'status': 'ok', 'value': r}

def ifc_set_entity_prop(p):
    """Set a direct IFC entity attribute. params: {object_id, prop, value}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    ok = vs.IFC_SetEntityProp(h, p.get('prop', ''), str(p.get('value', '')))
    return {'status': 'ok', 'set': bool(ok)}

def ifc_bulk_set_pset(p):
    """Set a pset property on EVERY object matching a criteria — the bulk tool
    for DIN276/classification pipelines. params: {criteria, pset, prop, value,
    entity? (also assign this IFC entity first, e.g. 'IfcSlab')}.
    Collect-then-mutate (safe against ForEachObject iteration corruption)."""
    crit = p.get('criteria')
    pset, prop = p.get('pset'), p.get('prop')
    if not crit or not pset or not prop:
        return {'error': 'criteria, pset, prop required'}
    val = str(p.get('value', ''))
    handles = []
    vs.ForEachObject(lambda hh: handles.append(hh), crit)
    done = 0; failed = 0
    for hh in handles:
        try:
            if p.get('entity'):
                vs.IFC_SetIFCEntity(hh, p['entity'])
            # SetPsetProp fails when the pset is not yet ATTACHED to this
            # object (verified live) — attach + retry on first failure.
            if not vs.IFC_SetPsetProp(hh, pset, prop, val):
                vs.IFC_AttachPset(hh, pset)
                if vs.IFC_SetPsetProp(hh, pset, prop, val): done += 1
                else: failed += 1
            else:
                done += 1
        except Exception:
            failed += 1
    return {'status': 'ok', 'matched': len(handles), 'set': done, 'failed': failed}

# ---- textures -----------------------------------------------------------------

def create_texture(p):
    """Create a texture resource (plain color shader attached; edit color in
    the Resource Manager or via shader records). params: {name, size?}."""
    t = vs.CreateTexture()
    if not t: return {'error': 'CreateTexture failed'}
    if p.get('name'): vs.SetName(t, p['name'])
    _safe(lambda: vs.CreateShaderRecord(t, 1, 30))  # family 1 = color, prototype 30 = plain color
    if p.get('size') is not None:
        _safe(lambda: vs.SetTextureSize(t, float(p['size'])))
    return {'status': 'ok', 'object_id': _oid(t), 'name': (p.get('name') or vs.GetName(t))}

def get_texture_info(p):
    """Texture resource info: size + shader. params: {texture(name)}."""
    t = vs.GetObject(p.get('texture', ''))
    if not t: return {'error': 'texture not found (by name)'}
    return {'status': 'ok', 'size': _safe(lambda: vs.GetTextureSize(t)),
            'shader': _safe(lambda: vs.GetTextureShader(t))}

def set_texture_size(p):
    """Real-world size of a texture. params: {texture(name), size}."""
    t = vs.GetObject(p.get('texture', ''))
    if not t: return {'error': 'texture not found (by name)'}
    vs.SetTextureSize(t, float(p.get('size', 1000)))
    return {'status': 'ok'}

def set_object_texture(p):
    """Apply a texture (by resource name) to an object part.
    params: {object_id, texture(name), part(0=overall 1=top 2=bottom 3=sides...),
    layer(0)}. texture:'' removes (ref 0)."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    name = p.get('texture', '')
    ref = 0
    if name:
        ref = vs.Name2Index(name)
        if not ref: return {'error': "texture '%s' not found (Name2Index=0)" % name}
    vs.SetTextureRefN(h, ref, int(p.get('part', 0)), int(p.get('layer', 0)))
    return {'status': 'ok', 'ref': ref}

def get_object_texture(p):
    """Texture applied to an object part. params: {object_id, part(0), layer(0),
    resolve_by_class(bool)}. Returns ref index + resource name."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    ref = vs.GetTextureRefN(h, int(p.get('part', 0)), int(p.get('layer', 0)),
                            bool(p.get('resolve_by_class', True)))
    name = vs.Index2Name(ref) if ref else ''
    return {'status': 'ok', 'ref': ref, 'texture': name}

def set_texture_mapping(p):
    """Texture mapping value on an object part (scale/rotation/offset).
    params: {object_id, part(0), layer(0), selector (SetTexMapRealN codes:
    1=offsetX 2=offsetY 3=rotation 4=scale2D ...), value}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    vs.SetTexMapRealN(h, int(p.get('part', 0)), int(p.get('layer', 0)),
                      int(p.get('selector', 4)), float(p.get('value', 1)))
    return {'status': 'ok'}

def get_texture_mapping(p):
    """Read a texture mapping value. params: {object_id, part(0), layer(0), selector}."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'object not found'}
    v = vs.GetTexMapRealN(h, int(p.get('part', 0)), int(p.get('layer', 0)),
                          int(p.get('selector', 4)))
    return {'status': 'ok', 'value': v}

# ---- document default attributes ------------------------------------------------

def set_default_attributes(p):
    """Document DEFAULT attributes for newly created objects (the attribute
    palette state). params (all optional): {fill_color:[r,g,b], fill_back:[r,g,b],
    pen_color:[r,g,b], pen_back:[r,g,b], line_weight (mils), fill_pattern (int,
    1=solid), pen_pattern (int, 2=solid line)}."""
    def idx(rgb):
        r = vs.RGBToColorIndex(int(rgb[0]) * 257, int(rgb[1]) * 257, int(rgb[2]) * 257)
        return r if isinstance(r, int) else (r[-1] if r else 0)
    applied = []
    if p.get('fill_color'):  vs.FillFore(idx(p['fill_color']));  applied.append('fill_color')
    if p.get('fill_back'):   vs.FillBack(idx(p['fill_back']));   applied.append('fill_back')
    if p.get('pen_color'):   vs.PenFore(idx(p['pen_color']));    applied.append('pen_color')
    if p.get('pen_back'):    vs.PenBack(idx(p['pen_back']));     applied.append('pen_back')
    if p.get('line_weight') is not None: vs.PenSize(int(p['line_weight'])); applied.append('line_weight')
    if p.get('fill_pattern') is not None: vs.FillPat(int(p['fill_pattern'])); applied.append('fill_pattern')
    if p.get('pen_pattern') is not None: vs.PenPatN(int(p['pen_pattern'])); applied.append('pen_pattern')
    return {'status': 'ok', 'applied': applied}

def set_default_text_style(p):
    """Document DEFAULT text attributes for new text objects. params (optional):
    {font(name), size(pt), justification (1=left 2=center 3=right),
    spacing (0=custom 2=single 3=1.5 4=double), face (0=plain 1=bold 2=italic)}."""
    applied = []
    if p.get('font'):
        fid = vs.GetFontID(p['font'])
        if fid < 0: return {'error': "font '%s' not found" % p['font']}
        vs.TextFont(fid); applied.append('font')
    if p.get('size') is not None: vs.TextSize(float(p['size'])); applied.append('size')
    if p.get('justification') is not None: vs.TextJust(int(p['justification'])); applied.append('justification')
    if p.get('spacing') is not None: vs.TextSpace(int(p['spacing'])); applied.append('spacing')
    if p.get('face') is not None: vs.TextFace(int(p['face'])); applied.append('face')
    return {'status': 'ok', 'applied': applied}

def set_default_marker(p):
    """Document DEFAULT arrowhead/marker for new dimensions/leaders.
    params: {style (0..15), size, angle(deg)}."""
    vs.Marker(int(p.get('style', 0)), float(p.get('size', 3)), int(p.get('angle', 15)))
    return {'status': 'ok'}


def boolean_operation(p):
    h1 = _h(p.get('object_id_a'))
    h2 = _h(p.get('object_id_b'))
    if not h1 or not h2: return {'error': 'Objects not found'}
    op = {'add': 0, 'subtract': 1, 'intersect': 2}.get(p.get('operation', 'add'), 0)
    try:
        nh = vs.CSGOperation(h1, h2, op)
        return {'status': 'ok', 'object_id': _oid(nh)}
    except Exception as e:
        return {'error': str(e)}

_NAMED_VIEW_ANGLES = {
    'top': (0.0, 0.0, 0.0),
    'front': (-90.0, 0.0, 0.0),
    'back': (90.0, 0.0, 180.0),
    'right': (0.0, 90.0, 0.0),
    'left': (0.0, -90.0, 0.0),
    'bottom': (180.0, 0.0, 0.0),
    'iso': (-60.0, 0.0, -45.0),
    'iso_right': (-60.0, 0.0, -45.0),
    'iso_left': (-60.0, 0.0, 45.0),
    'trimetric': (-60.0, 0.0, -15.0),
}


def _named_view_angles(view):
    return _NAMED_VIEW_ANGLES.get(str(view or '').lower())


def set_3d_view(p):
    """Set a named VW2026 view using SetView's verified six-argument signature."""
    view = str(p.get('view') or 'top').lower()
    angles = _named_view_angles(view)
    if not angles:
        return {'error': 'unknown view', 'view': view,
                'valid_views': sorted(_NAMED_VIEW_ANGLES)}
    if not vcheck('SetView', 6):
        return {'error': 'SetView signature index does not accept six arguments'}
    vs.SetView(angles[0], angles[1], angles[2], 0.0, 0.0, 0.0)
    return {'status': 'ok', 'view': view,
            'angles': {'x': angles[0], 'y': angles[1], 'z': angles[2]},
            'offset': {'x': 0.0, 'y': 0.0, 'z': 0.0}}


# ── Symbols ─────────────────────────────────────────────────────────────────

def get_symbols(p):
    syms = []
    for h in _collect('T=SYMDEF'):
        syms.append({'name': _safe(lambda: vs.GetName(h)), 'object_id': _oid(h)})
    return {'symbols': syms, 'count': len(syms)}

def place_symbol(p):
    prev = _with_layer_class(p)
    try:
        vs.Symbol(p.get('name', ''), (p.get('x', 0.0), p.get('y', 0.0)),
                  p.get('angle', 0.0))
        h = vs.LNewObj()
        if h and p.get('scale', 1.0) != 1.0:
            bb = _bbox(h)
            cx = (bb['x1']+bb['x2'])/2 if bb else p.get('x', 0)
            cy = (bb['y1']+bb['y2'])/2 if bb else p.get('y', 0)
            s = float(p.get('scale', 1.0))
            vs.HScale2D(h, cx, cy, s, s, True)
        return {'status': 'ok', 'object_id': _oid(h)}
    finally:
        _restore(prev)

def get_symbol_instances(p):
    name = p.get('name', '')
    hs = _collect(f"(T=SYMBOL) & (S='{name}')")
    return {'objects': [_summary(h) for h in hs], 'count': len(hs)}

def create_symbol_from_objects(p):
    # VW2026: no SymbolCreate. BeginSym/EndSym captures objects CREATED between
    # the calls into a new symbol def; duplicate the source objects into that
    # scope so the originals stay on the drawing.
    name = p.get('name', 'NewSymbol')
    ids = p.get('object_ids', [])
    if not ids:
        return {'error': 'object_ids required'}
    vs.BeginSym(name)
    made = 0
    for oid in ids:
        h = _h(oid)
        if h:
            d = _safe(lambda h=h: vs.HDuplicate(h, 0, 0))
            if d:
                made += 1
    vs.EndSym()
    if made == 0:
        return {'error': 'no source objects resolved'}
    return {'status': 'ok', 'name': name, 'objects': made}

def delete_symbol(p):
    name = p.get('name', '')
    h = _safe(lambda: vs.GetObject(name))
    if not h: return {'error': f'Symbol not found: {name}'}
    vs.DelObject(h)
    return {'status': 'ok'}

def rename_symbol(p):
    old = p.get('old_name', '')
    new = p.get('new_name', '')
    h = _safe(lambda: vs.GetObject(old))
    if not h: return {'error': f'Symbol not found: {old}'}
    vs.SetName(h, new)
    return {'status': 'ok'}


# ── Appearance ──────────────────────────────────────────────────────────────

def set_fill_color(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    col = (_c8(p.get('r',255)), _c8(p.get('g',255)), _c8(p.get('b',255)))
    vs.SetFillFore(h, col)
    vs.SetFillBack(h, col)
    vs.SetFPat(h, 1)
    return {'status': 'ok'}

def set_pen_color(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    col = (_c8(p.get('r',0)), _c8(p.get('g',0)), _c8(p.get('b',0)))
    vs.SetPenFore(h, col)
    vs.SetPenBack(h, col)
    return {'status': 'ok'}

def set_line_weight(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    # VW lineweight uses 0.01 mm increments ("mil" units)
    mm = float(p.get('weight_mm', 0.25))
    vs.SetLW(h, int(mm * 100))
    return {'status': 'ok'}

def set_fill_pattern(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    vs.SetFPat(h, int(p.get('pattern', 1)))
    return {'status': 'ok'}

def set_opacity(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    if p.get('fill_opacity') is not None:
        vs.SetOpacity(h, int(p['fill_opacity']))
    if p.get('pen_opacity') is not None:
        _safe(lambda: vs.SetObjectVariableInt(h, 540, int(p['pen_opacity'])))
    return {'status': 'ok'}

def get_appearance(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    rf = _safe(lambda: vs.GetFillFore(h))
    rp = _safe(lambda: vs.GetPenFore(h))
    return {
        'fill_color': [_c255(v) for v in rf] if rf else None,
        'pen_color':  [_c255(v) for v in rp] if rp else None,
        'fill_pattern': _safe(lambda: vs.GetFPat(h)),
        'lineweight':  _safe(lambda: vs.GetLW(h)),
        'opacity':     _safe(lambda: vs.GetOpacity(h)),
    }

def set_marker(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    mmap = {'none': 0, 'arrow': 1, 'open_arrow': 2, 'dot': 5, 'slash': 6}
    s = mmap.get(p.get('start_marker', ''), None)
    e = mmap.get(p.get('end_marker', ''), None)
    try:
        if s is not None: vs.SetObjBeginningMarker(h, s, 0.1, 1.0, True, True)
        if e is not None: vs.SetObjEndMarker(h, e, 0.1, 1.0, True, True)
        return {'status': 'ok'}
    except Exception as ex:
        return {'error': str(ex)}


# ── Records ─────────────────────────────────────────────────────────────────

def get_record_formats(p):
    fmts = []
    for h in _collect('T=RECDEF'):
        n = _safe(lambda: vs.GetName(h))
        if n:
            fmts.append({'name': n, 'object_id': _oid(h),
                         'field_count': _safe(lambda: vs.NumFields(h), 0)})
    return {'formats': fmts, 'count': len(fmts)}

def get_object_records(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    out = {}
    try:
        n = vs.NumRecords(h)
        for i in range(1, n + 1):
            rh = vs.GetRecord(h, i)
            if not rh: continue
            rec_name = vs.GetName(rh)
            fields = {}
            for j in range(1, vs.NumFields(rh) + 1):
                fn = _safe(lambda: vs.GetFldName(rh, j), f'f{j}')
                fields[fn] = _safe(lambda: vs.GetRField(h, rec_name, fn), '')
            out[rec_name] = fields
        return {'records': out, 'count': len(out)}
    except Exception as e:
        return {'error': str(e)}

def get_record_field(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    v = _safe(lambda: vs.GetRField(h, p.get('record_name', ''), p.get('field_name', '')), '')
    return {'value': v}

def set_record_field(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    vs.SetRField(h, p.get('record_name', ''), p.get('field_name', ''),
                 str(p.get('value', '')))
    return {'status': 'ok'}

def attach_record(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    vs.SetRecord(h, p.get('record_name', ''))
    return {'status': 'ok'}

def detach_record(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        vs.RemoveRecord(h, p.get('record_name', ''))
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}

def create_record_format(p):
    # vs.NewField on a NON-EXISTING format PARKS the script frame in a nested
    # message loop on VW2026 (verified live 2026-07-06 — killed the sweep and,
    # earlier, the Produktlink batch). Adding fields to an EXISTING format is
    # fine. Create the format the safe way: attach it to a temp object via
    # BeginRecord-less path is not scriptable, so use vs.NewField ONLY when
    # the format already exists; otherwise create it via a temporary locus +
    # record attach through the resource list.
    name = p.get('name', '')
    fields = p.get('fields', [])
    type_map = {'string': 4, 'text': 4, 'integer': 1, 'number': 3,
                'real': 3, 'boolean': 2}
    if not name:
        return {'error': 'name required'}
    existed = bool(_safe(lambda: vs.GetObject(name)))
    if not existed:
        return {'error': "create_record_format is quarantined for NEW formats "
                         "on VW2026 — vs.NewField parks the script frame when "
                         "the format doesn't exist yet. Create the format once "
                         "in the GUI (Werkzeuge > Datenbank) or attach fields "
                         "to an existing format."}
    for f in fields:
        vs.NewField(name, f.get('name', ''), str(f.get('default', '')),
                    type_map.get(f.get('type', 'string'), 4), 0)
    return {'status': 'ok', 'name': name, 'existed': existed,
            'fields_added': len(fields)}


# ── IFC / BIM ───────────────────────────────────────────────────────────────

def get_ifc_entity(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        ok, entity = vs.IFC_GetIFCEntity(h)
        return {'entity': entity if ok else '', 'ok': ok}
    except Exception as e:
        return {'error': str(e)}

def set_ifc_entity(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        vs.IFC_SetIFCEntity(h, p.get('entity', 'IfcBuildingElement'), '', '')
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}

def get_ifc_properties(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        # IFC property enumeration — best-effort via IFC_GetPSetAttribute iteration
        psets = {}
        # No full enumeration API; return placeholder with entity
        ok, entity = vs.IFC_GetIFCEntity(h)
        return {'entity': entity if ok else '', 'psets': psets,
                'note': 'Full pset enumeration requires IFC_PSetList API'}
    except Exception as e:
        return {'error': str(e)}

def set_ifc_property(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        # VW2026: IFC_SetPsetProp(h, pset, prop, value:STRING)
        pset = p.get('pset', '')
        prop = p.get('prop') or p.get('name', '')
        val = str(p.get('value', ''))
        ok = vs.IFC_SetPsetProp(h, pset, prop, val)
        if not ok:
            # fails when the pset is not yet attached to this object — attach + retry
            vs.IFC_AttachPset(h, pset)
            ok = vs.IFC_SetPsetProp(h, pset, prop, val)
        return {'status': 'ok' if ok else 'error'}
    except Exception as e:
        return {'error': str(e)}

def export_ifc(p):
    path = p.get('path', '')
    try:
        ok = vs.IFC_ExportNoUI(path)
        return {'status': 'ok' if ok else 'error', 'path': path}
    except Exception as e:
        return {'error': str(e)}


# ── Architectural ───────────────────────────────────────────────────────────

def _wall_trace(step):
    # create_wall hard-crashed VW2026 (sweep 2026-07-06). Each step is traced
    # so a future crash names its exact line in bridge.log.
    try:
        import os, time
        d = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(d, 'bridge.log'), 'a', encoding='utf-8') as f:
            f.write('[%s] create_wall: %s\n' % (time.strftime('%H:%M:%S'), step))
    except Exception:
        pass

def create_wall(p):
    # v11 rewrite: documented wall APIs only. The old version pushed magic
    # prefs (vs.SetPrefReal(85), vs.SetPref(68)) before vs.Wall and killed
    # VW hard. Now: SetWallWidth (documented default-width setter) -> Wall ->
    # per-wall SetWallThickness/SetWallHeights on the unstyled wall.
    prev = _with_layer_class(p)
    try:
        height = p.get('height', 2500)
        thick  = p.get('thickness', 200)
        _wall_trace('begin h=%s t=%s' % (height, thick))
        _safe(lambda: vs.SetWallWidth(thick))          # document default width
        _wall_trace('SetWallWidth ok')
        vs.Wall((p.get('x1',0), p.get('y1',0)),
                (p.get('x2',1000), p.get('y2',0)))
        _wall_trace('Wall ok')
        h = vs.LNewObj()
        _wall_trace('LNewObj ok h=%s' % bool(h))
        if h:
            _safe(lambda: vs.SetWallThickness(h, thick))
            _wall_trace('SetWallThickness ok')
            _safe(lambda: vs.SetWallHeights(h, height, height))
            _wall_trace('SetWallHeights ok')
        _wall_trace('done')
        return {'status': 'ok', 'object_id': _oid(h)}
    finally:
        _restore(prev)

def create_space(p):
    prev = _with_layer_class(p)
    try:
        ids = p.get('boundary_ids') or []
        vs.DSelectAll()
        for oid in ids:
            hh = _h(oid)
            if hh: vs.SetSelect(hh)
        vs.DoMenuTextByName('Space from Polyline', 0)
        h = vs.LNewObj()
        if h and p.get('name'):
            vs.SetName(h, p['name'])
        return {'status': 'ok', 'object_id': _oid(h)}
    finally:
        _restore(prev)

def get_spaces(p):
    hs = _collect('T=SPACE')
    out = []
    for h in hs:
        s = _summary(h)
        s['area']      = _safe(lambda: vs.GetObjectVariableReal(h, 602))
        s['perimeter'] = _safe(lambda: vs.GetObjectVariableReal(h, 603))
        out.append(s)
    return {'spaces': out, 'count': len(out)}

def get_walls(p):
    parts = ['T=WALL']
    if p.get('layer'): parts.append(f"L='{p['layer']}'")
    hs = _collect(' & '.join(parts))
    out = []
    for h in hs:
        s = _summary(h)
        s['height']    = _safe(lambda: vs.GetObjectVariableReal(h, 173))
        s['thickness'] = _safe(lambda: vs.GetWallThickness(h))
        out.append(s)
    return {'walls': out, 'count': len(out)}


# ── Landscape / Plants ──────────────────────────────────────────────────────

def get_plants(p):
    parts = ['T=PLUGINOBJ']
    if p.get('layer'): parts.append(f"L='{p['layer']}'")
    hs = _collect(' & '.join(parts), p.get('limit', 500))
    plants = []
    for h in hs:
        # Keep only actual plant plugin objects
        plugin_name = _safe(lambda: vs.GetPluginType(h), '') or ''
        if 'plant' not in plugin_name.lower() and 'pflanz' not in plugin_name.lower():
            continue
        s = _summary(h)
        s['plugin_name'] = plugin_name
        prec = _safe(lambda: vs.GetParametricRecord(h))
        if prec:
            rec_name = _safe(lambda: vs.GetName(prec), '')
            s['record_name'] = rec_name
            s['plant_fields'] = {}
            for i in range(1, _safe(lambda: vs.NumFields(prec), 0) + 1):
                fn = _safe(lambda: vs.GetFldName(prec, i), f'f{i}')
                s['plant_fields'][fn] = _safe(
                    lambda: vs.GetRField(h, rec_name, fn), '')
        plants.append(s)
    return {'plants': plants, 'count': len(plants)}

def create_plant(p):
    prev = _with_layer_class(p)
    try:
        name = p.get('botanical_name') or p.get('common_name') or 'Plant'
        try:
            vs.Symbol(name, (p.get('x', 0), p.get('y', 0)), 0)
            h = vs.LNewObj()
            return {'status': 'ok', 'object_id': _oid(h)}
        except Exception:
            return {'error': f'Plant symbol not found: {name}'}
    finally:
        _restore(prev)

def update_plant(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Plant not found'}
    prec = _safe(lambda: vs.GetParametricRecord(h))
    if not prec: return {'error': 'No parametric record'}
    rec = _safe(lambda: vs.GetName(prec), '')
    mapping = {
        'botanical_name': 'Botanischer Name',
        'common_name':    'Deutscher Name',
        'height':         'Höhe',
        'spread':         'Kronendurchmesser',
    }
    updated = 0
    for k, v in mapping.items():
        if p.get(k) is not None:
            _safe(lambda: vs.SetRField(h, rec, v, str(p[k])))
            updated += 1
    for fn, val in (p.get('extra_fields') or {}).items():
        _safe(lambda: vs.SetRField(h, rec, fn, str(val)))
        updated += 1
    return {'status': 'ok', 'updated': updated}

def get_plant_database(p):
    # Plant DB lives as symbols in the Resource Manager
    plants = []
    for h in _collect('T=SYMDEF'):
        n = _safe(lambda: vs.GetName(h), '')
        if n and ('plant' in n.lower() or 'pflanz' in n.lower() or 'baum' in n.lower()):
            plants.append({'name': n})
    return {'plants': plants, 'count': len(plants)}

def batch_update_plants(p):
    """updates: [{object_id, field_name, value, record_name?}, ...]"""
    updates = p.get('updates', [])
    done = 0
    for u in updates:
        h = _h(u.get('object_id'))
        if not h: continue
        rec = u.get('record_name')
        if not rec:
            prec = _safe(lambda: vs.GetParametricRecord(h))
            rec = _safe(lambda: vs.GetName(prec), '') if prec else 'Plant Record'
        try:
            vs.SetRField(h, rec, u.get('field_name', ''), str(u.get('value', '')))
            done += 1
        except Exception:
            pass
    return {'status': 'ok', 'updated': done}


# ── Site Model ──────────────────────────────────────────────────────────────

def get_site_model_info(p):
    hs = _collect('T=DTM') or _collect('T=STAKE')
    if not hs: return {'error': 'No site model in document'}
    h = hs[0]
    return {
        'object_id': _oid(h),
        'name':      _safe(lambda: vs.GetName(h)),
        'bounds':    _bbox(h),
    }

def update_site_model(p):
    hs = _collect('T=DTM')
    for h in hs:
        _safe(lambda: vs.ResetObject(h))
    return {'status': 'ok', 'updated': len(hs)}

def get_terrain_elevation(p):
    try:
        z = vs.GetZFromSiteModel(p.get('x', 0), p.get('y', 0))
        return {'elevation': z}
    except Exception as e:
        return {'error': str(e)}


# ── Viewports ───────────────────────────────────────────────────────────────

def get_viewports(p):
    hs = _collect('T=VIEWPORT')
    vps = []
    for h in hs:
        s = _summary(h)
        s['scale'] = _safe(lambda: vs.GetObjectVariableReal(h, 1003))
        vps.append(s)
    return {'viewports': vps, 'count': len(vps)}

def create_viewport(p):
    """Create a viewport directly under a sheet layer without activating it.

    VW2026 CreateVP takes exactly one parent handle. The previous wrapper called
    quarantined vs.Layer() and passed three arguments to CreateVP, so it could hang
    the bridge and never create a viewport.
    """
    try:
        sheet = vs.GetLayerByName(str(p.get('sheet_layer') or ''))
        if not sheet or int(_safe(lambda: vs.GetObjectVariableInt(sheet, 154), 0) or 0) != 2:
            return {'error': 'sheet_layer must name an existing sheet layer'}
        h = vs.CreateVP(sheet)
        if not h: return {'error': 'CreateVP failed'}
        name = str(p.get('name') or '').strip()
        if name:
            if _safe(lambda: vs.GetObject(name)):
                vs.DelObject(h)
                return {'error': 'name already in use: %s' % name}
            vs.SetName(h, name)
        vs.SetObjectVariableReal(h, 1003, float(p.get('scale', 100)))
        if p.get('projection') is not None:
            vs.SetObjectVariableInt(h, 1000, int(p['projection']))
        if p.get('view_type') is not None:
            vs.SetObjectVariableInt(h, 1007, int(p['view_type']))
        visible = set(str(value) for value in (p.get('design_layers') or []))
        if visible:
            for row in get_layers({}).get('layers') or []:
                if row.get('type') != 'design': continue
                layer = vs.GetLayerByName(row['name'])
                vs.SetVPLayerVisibility(h, layer, 0 if row['name'] in visible else -1)
        vs.UpdateVP(h)
        p1, p2 = vs.GetBBox(h)
        cx = (p1[0] + p2[0]) / 2.0; cy = (p1[1] + p2[1]) / 2.0
        vs.HMove(h, float(p.get('x', 0)) - cx, float(p.get('y', 0)) - cy)
        return {'status': 'ok', 'object_id': _oid(h), 'name': name or None,
                'type': int(vs.GetTypeN(h)),
                'sheet_layer': str(_safe(lambda: vs.GetLName(vs.GetLayer(h))) or ''),
                'scale': float(vs.GetObjectVariableReal(h, 1003))}
    except Exception as e:
        return {'error': str(e)}

def update_viewport(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Viewport not found'}
    vs.UpdateVP(h)
    return {'status': 'ok'}

def set_viewport_scale(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Viewport not found'}
    vs.SetObjectVariableReal(h, 1003, float(p.get('scale', 100)))
    _safe(lambda: vs.UpdateVP(h))
    return {'status': 'ok'}

def set_viewport_crop(p):
    h = _h(p.get('object_id'))
    crop = _h(p.get('crop_object_id'))
    if not h or not crop: return {'error': 'Viewport or crop not found'}
    try:
        vs.SetVPCropObject(h, crop)
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}


# ── Worksheets ──────────────────────────────────────────────────────────────

def get_worksheets(p):
    hs = _collect('T=WORKSHEET')
    return {'worksheets': [{'object_id': _oid(h),
                            'name': _safe(lambda h=h: vs.GetName(h))}
                           for h in hs], 'count': len(hs)}

def create_worksheet(p):
    name = p.get('name', 'Worksheet')
    try:
        h = vs.CreateWS(name, 10, 5)
        return {'status': 'ok', 'object_id': _oid(h)}
    except Exception as e:
        return {'error': str(e)}

def get_worksheet_data(p):
    ws = _safe(lambda: vs.GetObject(p.get('name', '')))
    if not ws: return {'error': 'Worksheet not found'}
    row_start = p.get('row_start', 1)
    row_end = p.get('row_end', 100)
    cols = 10
    data = []
    for r in range(row_start, row_end + 1):
        row = []
        for c in range(1, cols + 1):
            row.append(_safe(lambda: vs.GetWSCellValue(ws, r, c), ''))
        data.append(row)
    return {'data': data, 'rows': len(data)}

def set_worksheet_cell(p):
    ws = _safe(lambda: vs.GetObject(p.get('name', '')))
    if not ws: return {'error': 'Worksheet not found'}
    # VW2026: no SetWSCellValue. SetWSCellFormula(ws, topR, leftC, botR, rightC, formula)
    r = p.get('row', 1); c = p.get('col', 1)
    vs.SetWSCellFormula(ws, r, c, r, c, str(p.get('value', '')))
    return {'status': 'ok'}

def recalculate_worksheet(p):
    ws = _safe(lambda: vs.GetObject(p.get('name', '')))
    if not ws: return {'error': 'Worksheet not found'}
    vs.RecalculateWS(ws)
    return {'status': 'ok'}


# ── Export / Import ─────────────────────────────────────────────────────────

def export_pdf(p):
    path = p.get('path', '')
    try:
        if vs.AcquireExportPDFSettingsAndLocation(False):
            if vs.OpenPDFDocument(path):
                vs.ExportPDFPages(p.get('pages', ''))
                vs.ClosePDFDocument()
                return {'status': 'ok', 'path': path}
        return {'error': 'PDF export cancelled'}
    except Exception as e:
        return {'error': str(e)}

def export_dxf(p):
    try:
        path = p.get('path', '')
        if path:
            _safe(lambda: vs.ExportDXFDWG_Batch(path))
            return {'status': 'ok', 'path': path}
        vs.ExportDXFDWG()
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}

def export_image(p):
    try:
        path = p.get('path', '')
        w = int(p.get('width', 2000))
        h = int(p.get('height', 1500))
        dpi = int(p.get('dpi', 150))
        fmt = p.get('format', 'png').lower()
        fmap = {'png': 4, 'jpg': 2, 'jpeg': 2, 'tif': 3, 'tiff': 3}
        vs.ExportImageFile(path, w, h, dpi, fmap.get(fmt, 4))
        return {'status': 'ok', 'path': path}
    except Exception as e:
        return {'error': str(e)}

def export_stl(p):
    """Headless STL export with explicit path and post-write verification."""
    path = str(p.get('path', '') or '').strip()
    if not path or not os.path.isabs(path):
        return {'error': 'absolute .stl path required'}
    if not path.lower().endswith('.stl'):
        return {'error': 'export path must end in .stl'}
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        return {'error': 'export directory does not exist: %s' % parent}
    try:
        tess = float(p.get('tessellation', 50.0))
    except (TypeError, ValueError):
        return {'error': 'tessellation must be a number from 0 to 100'}
    if not 0.0 <= tess <= 100.0:
        return {'error': 'tessellation must be from 0 to 100'}
    scope = str(p.get('scope', 'selected') or 'selected').lower()
    options = {'selected': 0, 'active_layer': 1, 'all': 2}
    if scope not in options:
        return {'error': 'scope must be selected, active_layer, or all'}
    if scope == 'selected' and not _collect('SEL=TRUE', 1):
        return {'error': 'no visible objects selected for STL export'}
    binary = bool(p.get('binary', True))
    try:
        ok = bool(vs.ExportSTL(path, binary, tess, options[scope]))
    except Exception as e:
        return {'error': str(e)}
    if not ok:
        return {'error': 'Vectorworks ExportSTL returned false', 'path': path}
    if not os.path.isfile(path):
        return {'error': 'Vectorworks reported success but no STL was written',
                'path': path}
    return {'status': 'ok', 'path': path, 'size': os.path.getsize(path),
            'binary': binary, 'tessellation': tess, 'scope': scope}

def import_dwg(p):
    try:
        vs.ImportDXFDWG(p.get('path', ''), False)
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}

def export_shp(p):
    try:
        vs.ExportSHP(p.get('path', ''))
        return {'status': 'ok', 'path': p.get('path')}
    except Exception as e:
        return {'error': str(e)}

def import_image(p):
    try:
        path = p.get('path', '')
        x, y = p.get('x', 0), p.get('y', 0)
        vs.ImportImageFile(path, (x, y))
        return {'status': 'ok', 'object_id': _oid(vs.LNewObj())}
    except Exception as e:
        return {'error': str(e)}


# ── View ────────────────────────────────────────────────────────────────────

def zoom_to_fit(p):
    try:
        vs.FitViewToObjects()
    except AttributeError:
        # VW2026: FitViewToObjects removed; fall back to menu command
        try:
            vs.DoMenuTextByName('Fit To Objects', 0)
        except Exception as e:
            return {'error': str(e)}
    return {'status': 'ok'}

def zoom_to_selection(p):
    # VW2026: no ZoomToSel. Drive the 'Fit To Objects' menu command, which
    # fits the current selection when objects are selected.
    _safe(lambda: vs.DoMenuTextByName('Fit To Objects', 0))
    _safe(lambda: vs.ReDrawAll())
    return {'status': 'ok'}

def set_zoom(p):
    try:
        vs.SetZoom(float(p.get('percent', 100)))
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}

def refresh_view(p):
    vs.ReDrawAll()
    return {'status': 'ok'}


# ── GIS ─────────────────────────────────────────────────────────────────────

def set_georeferencing(p):
    try:
        vs.SetDocumentGeoreferenceEPSG(p.get('crs', 'EPSG:25832'))
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}

def get_georeferencing(p):
    # No vs.GetDocumentGeoreferenceEPSG on VW2026. Probe the APIs that do
    # exist across builds; report honestly what was found.
    out = {}
    for fname, key in (('GetGeoOrigin', 'geo_origin'),
                       ('GetGeorefMode', 'georef_mode'),
                       ('GetGISProjectionStr', 'projection')):
        f = getattr(vs, fname, None)
        if f:
            v = _safe(f)
            if v is not None:
                out[key] = v
    # layer-level georeferencing flag
    h = vs.ActLayer()
    if h:
        out['active_layer_georef'] = _safe(lambda: vs.GetObjectVariableBoolean(h, 172))
    if not out:
        return {'error': 'no georeferencing API found on this VW build '
                         '(available vs.* names probed: GetGeoOrigin, '
                         'GetGeorefMode, GetGISProjectionStr)'}
    out['status'] = 'ok'
    return out


# ── Textures ────────────────────────────────────────────────────────────────

def get_textures(p):
    txs = []
    for h in _collect('T=TEXTURE'):
        txs.append({'name': _safe(lambda: vs.GetName(h)),
                    'object_id': _oid(h)})
    return {'textures': txs, 'count': len(txs)}

def apply_texture(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        tx = vs.GetObject(p.get('texture_name', ''))
        if not tx: return {'error': 'Texture not found'}
        vs.SetTextureRef(h, tx, 0, 1)
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}


# ── Alignment / Distribution ────────────────────────────────────────────────

def _bbox_pairs(ids):
    out = []
    for oid in ids or []:
        h = _h(oid)
        if not h: continue
        try:
            p1, p2 = vs.GetBBox(h)
            out.append((h, p1, p2))
        except Exception: pass
    return out

def align_objects(p):
    """Align objects. mode: left,right,top,bottom,center_x,center_y,center.
    ref: optional UUID of reference; else uses aggregate bbox of the set."""
    ids = p.get('object_ids', [])
    mode = p.get('mode', 'center_x')
    ref = p.get('ref')
    items = _bbox_pairs(ids)
    if not items: return {'error': 'No valid objects'}
    if ref:
        rh = _h(ref)
        if not rh: return {'error': 'ref not found'}
        rp1, rp2 = vs.GetBBox(rh)
    else:
        xs1 = [q1[0] for _, q1, _ in items]; xs2 = [q2[0] for _, _, q2 in items]
        ys1 = [q1[1] for _, q1, _ in items]; ys2 = [q2[1] for _, _, q2 in items]
        rp1 = (min(xs1), min(ys1)); rp2 = (max(xs2), max(ys2))
    rcx = (rp1[0] + rp2[0]) / 2.0
    rcy = (rp1[1] + rp2[1]) / 2.0
    moved = 0
    for h, q1, q2 in items:
        cx = (q1[0] + q2[0]) / 2.0
        cy = (q1[1] + q2[1]) / 2.0
        dx = dy = 0.0
        if   mode == 'left':     dx = rp1[0] - q1[0]
        elif mode == 'right':    dx = rp2[0] - q2[0]
        elif mode == 'top':      dy = rp2[1] - q2[1]
        elif mode == 'bottom':   dy = rp1[1] - q1[1]
        elif mode == 'center_x': dx = rcx - cx
        elif mode == 'center_y': dy = rcy - cy
        elif mode == 'center':   dx = rcx - cx; dy = rcy - cy
        else: return {'error': f'unknown mode: {mode}'}
        if dx or dy:
            vs.HMove(h, dx, dy)
            moved += 1
    vs.ReDrawAll()
    return {'status': 'ok', 'moved': moved}

def distribute_objects(p):
    """Evenly distribute centers along x or y between the outermost two objects.
    Needs 3+ objects."""
    ids = p.get('object_ids', [])
    axis = p.get('axis', 'x')
    items = _bbox_pairs(ids)
    if len(items) < 3: return {'error': 'need 3+ objects'}
    def ctr(item):
        _, q1, q2 = item
        return (q1[0] + q2[0]) / 2.0 if axis == 'x' else (q1[1] + q2[1]) / 2.0
    items.sort(key=ctr)
    first_c = ctr(items[0]); last_c = ctr(items[-1])
    step = (last_c - first_c) / (len(items) - 1)
    moved = 0
    for i, it in enumerate(items[1:-1], start=1):
        h, q1, q2 = it
        target = first_c + step * i
        d = target - ctr(it)
        if axis == 'x': vs.HMove(h, d, 0)
        else:           vs.HMove(h, 0, d)
        moved += 1
    vs.ReDrawAll()
    return {'status': 'ok', 'moved': moved}


# ── Text Style ──────────────────────────────────────────────────────────────

def set_text_style(p):
    """Set text attrs. style flags: 1=bold 2=italic 4=underline (sum)."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        if p.get('font') is not None:
            fi = vs.GetFontID(p['font'])
            vs.SetTextFont(h, 0, -1, fi)
        if p.get('size') is not None:
            vs.SetTextSize(h, 0, -1, float(p['size']))
        if p.get('style') is not None:
            vs.SetTextStyle(h, 0, -1, int(p['style']))
        if p.get('justify') is not None:
            jmap = {'left': 1, 'center': 2, 'right': 3}
            vs.SetTextJust(h, jmap.get(p['justify'], 1))
        if all(k in p for k in ('r', 'g', 'b')):
            # VW2026 has no SetTextFill; text color is the object's fill color.
            col = (_c8(p['r']), _c8(p['g']), _c8(p['b']))
            vs.SetFillFore(h, col)
            vs.SetFillBack(h, col)
            vs.SetFPat(h, 1)
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}


# ── Generic Object Variable Access ──────────────────────────────────────────

def set_object_variable(p):
    """Generic ObjectVariable setter. type: int, bool, real, str."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    idx = int(p.get('index', 0))
    vtype = p.get('type', 'int')
    val = p.get('value')
    try:
        if   vtype == 'int':  vs.SetObjectVariableInt(h, idx, int(val))
        elif vtype == 'bool': vs.SetObjectVariableBoolean(h, idx, bool(val))
        elif vtype == 'real': vs.SetObjectVariableReal(h, idx, float(val))
        elif vtype == 'str':  vs.SetObjectVariableString(h, idx, str(val))
        else: return {'error': f'unknown type: {vtype}'}
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}

def get_object_variable(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    idx = int(p.get('index', 0))
    vtype = p.get('type', 'int')
    try:
        if   vtype == 'int':  v = vs.GetObjectVariableInt(h, idx)
        elif vtype == 'bool': v = vs.GetObjectVariableBoolean(h, idx)
        elif vtype == 'real': v = vs.GetObjectVariableReal(h, idx)
        elif vtype == 'str':  v = vs.GetObjectVariableString(h, idx)
        else: return {'error': f'unknown type: {vtype}'}
        return {'value': v}
    except Exception as e:
        return {'error': str(e)}


# ── Criteria Query ───────────────────────────────────────────────────────────

def for_each_criteria(p):
    """vs.ForEachObject criteria string (e.g. T=RECT, (L='Layer-1') & (T=POLY)).

    Gotcha: when building criteria with a variable, do NOT add extra outer parens
    or ForEachObject silently matches nothing and never errors.
      good: \"((R in ['Part Info']))\"
      bad : \"(((R in ['Part Info'])))\"
    Single-quote record/class/layer names that contain spaces."""
    criteria = p.get('criteria', '')
    limit = int(p.get('limit', 500))
    handles = _collect(criteria, limit)
    return {
        'count': len(handles),
        'ids': [_oid(h) for h in handles],
        'summaries': [_summary(h) for h in handles[:20]],
    }


# ── Baumkataster Bulk Record Setter ──────────────────────────────────────────

def baumkataster_set_fields(p):
    """Bulk-set record fields. fields: {FieldName: value}. Default record: Baumkataster."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    rec = p.get('record', 'Baumkataster')
    fields = p.get('fields', {}) or {}
    # Attach if not already attached
    try:
        vs.SetRecord(h, rec)
    except Exception: pass
    done = 0; errs = {}
    for k, v in fields.items():
        try:
            vs.SetRField(h, rec, k, str(v))
            done += 1
        except Exception as e:
            errs[k] = str(e)
    try: vs.ResetObject(h)
    except Exception: pass
    return {'status': 'ok', 'set': done, 'errors': errs}


# ── Extra 2D Primitives ──────────────────────────────────────────────────────

def draw_rounded_rect(p):
    """Polyline with arc-type vertices at each corner (VW 2D arc vertex flag = 3)."""
    prev = _with_layer_class(p)
    try:
        x1 = float(p.get('x1', 0));  y1 = float(p.get('y1', 0))
        x2 = float(p.get('x2', 100)); y2 = float(p.get('y2', 100))
        r = float(p.get('radius', 10))
        vs.ClosePoly()
        vs.BeginPoly()
        # Rect corners in CCW order
        for pt in [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]:
            vs.Add2DVertex(pt, 3, r)
        vs.EndPoly()
        return _newobj_result(p)
    finally:
        _restore(prev)

def draw_regular_polygon(p):
    """Regular n-gon inscribed in circle of given radius.
    rotation_deg=90 → vertex at top (pointy); 0 → vertex at right."""
    import math as _m
    prev = _with_layer_class(p)
    try:
        cx = float(p.get('cx', 0)); cy = float(p.get('cy', 0))
        r = float(p.get('radius', 100))
        n = max(3, int(p.get('sides', 3)))
        rot = _m.radians(float(p.get('rotation_deg', 90)))
        pts = [(cx + r * _m.cos(rot + 2 * _m.pi * i / n),
                cy + r * _m.sin(rot + 2 * _m.pi * i / n)) for i in range(n)]
        vs.ClosePoly()
        vs.BeginPoly()
        for x, y in pts:
            vs.Add2DVertex((x, y), 0, 0)
        vs.EndPoly()
        return _newobj_result(p)
    finally:
        _restore(prev)


# ── PIO (Plug-in Object) creation ────────────────────────────────────────────

def _layer_uuids():
    """Snapshot every UUID on the active layer. Used by path-PIO creation
    because CreateCustomObjectPath never triggers IsNewCustomObject TRUE and
    vs.LNewObj() does NOT return the new PIO — pre/post UUID diff is the
    reliable way to capture the freshly created handle."""
    seen = set()
    h = vs.FActLayer()
    while h:
        try:
            u = vs.GetObjectUuid(h)
            if u: seen.add(u)
        except Exception: pass
        h = vs.NextObj(h)
    return seen

def _apply_pio_params(h, name, parameters):
    if not parameters: return
    for field, value in parameters.items():
        try: vs.SetRField(h, name, field, str(value))
        except Exception: pass
    try: vs.ResetObject(h)
    except Exception: pass

def create_pio(p):
    """Create a Plug-in Object (Door, Window, Stair, Fence, Hardscape, Data Tag, …).

    Doors/Windows/Stairs are NOT first-class creators — they are PIOs. Use
    CreateCustomObjectN with the plug-in's INTERNAL name (e.g. 'Door', 'Window',
    'Stair', 'Data Tag', 'Fence', 'Hardscape'). Parameters are set via record
    fields on the record whose name == the PIO name, using the OIP field names
    (not internal pName). show_pref controls whether the object-preferences
    dialog is shown (default False)."""
    prev = _with_layer_class(p)
    try:
        name = p.get('name')
        if not name: return {'error': 'name (PIO internal name) required'}
        x = float(p.get('x', 0)); y = float(p.get('y', 0))
        rot = float(p.get('rotation', 0))
        show_pref = bool(p.get('show_pref', False))
        try:
            h = vs.CreateCustomObjectN(name, (x, y), rot, show_pref)
        except AttributeError:
            h = vs.CreateCustomObject(name, (x, y), rot)
        if not h:
            return {'error': f'PIO "{name}" not created — check plug-in is installed/enabled'}
        _apply_pio_params(h, name, p.get('parameters'))
        return _newobj_result(p, fallback=h)
    finally:
        _restore(prev)

def create_pio_from_path(p):
    """Create a path-based PIO (Fence, Hardscape, Planting bed, …).

    vs.CreateCustomObjectPath does NOT advance LNewObj() and does NOT fire
    IsNewCustomObject TRUE. Fallback strategy: snapshot active-layer UUIDs
    before and after the call, then take the single new UUID."""
    prev = _with_layer_class(p)
    try:
        name = p.get('name')
        path_oid = p.get('path_id')
        if not name or not path_oid:
            return {'error': 'name and path_id required'}
        path_h = _h(path_oid)
        if not path_h: return {'error': 'path not found'}
        pg_oid = p.get('profile_group_id')
        pg_h = _h(pg_oid) if pg_oid else None
        before = _layer_uuids()
        try:
            h = vs.CreateCustomObjectPath(name, path_h, pg_h)
        except Exception as e:
            return {'error': f'CreateCustomObjectPath failed: {e}'}
        new_h = h
        if not new_h:
            after = _layer_uuids() - before
            if after:
                new_h = _h(next(iter(after)))
        if not new_h:
            return {'error': 'PIO created but handle could not be resolved'}
        _apply_pio_params(new_h, name, p.get('parameters'))
        return {'status': 'ok', 'object_id': _oid(new_h)}
    finally:
        _restore(prev)


def get_pio_parameters(p):
    """Read all PIO parameter fields (PIO name == record name on the object)."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        rec_h = vs.GetParametricRecord(h)
        if not rec_h: return {'error': 'Not a PIO (no parametric record)'}
        rec_name = _safe(lambda: vs.GetName(rec_h))
        nf = _safe(lambda: vs.NumFields(rec_h), 0)
        out = {}
        for i in range(1, nf + 1):
            fname = _safe(lambda i=i: vs.GetFldName(rec_h, i))
            val = _safe(lambda fname=fname: vs.GetRField(h, rec_name, fname))
            if fname: out[fname] = val
        return {'record': rec_name, 'fields': out}
    except Exception as e:
        return {'error': str(e)}

def set_pio_parameter(p):
    """Set one PIO parameter field. Also ResetObject to trigger regen."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    field = p.get('field'); value = p.get('value')
    if not field: return {'error': 'field required'}
    try:
        rec_h = vs.GetParametricRecord(h)
        rec_name = vs.GetName(rec_h) if rec_h else p.get('record')
        if not rec_name: return {'error': 'record name unknown'}
        vs.SetRField(h, rec_name, field, str(value))
        vs.ResetObject(h)
        return {'status': 'ok', 'record': rec_name, 'field': field}
    except Exception as e:
        return {'error': str(e)}

def get_pio_style(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        name = vs.GetPluginStyle(h)
        return {'style': name}
    except Exception as e:
        return {'error': str(e)}

def set_pio_style(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    style = p.get('style_name')
    if not style: return {'error': 'style_name required'}
    try:
        vs.SetPluginStyle(h, style)
        vs.ResetObject(h)
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}

def update_all_styled_instances(p):
    style = p.get('style_name')
    if not style: return {'error': 'style_name required'}
    try:
        vs.UpdateStyledObjects(style)
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}

def has_plugin(p):
    name = p.get('name')
    if not name: return {'error': 'name required'}
    try:
        return {'exists': bool(vs.HasPlugin(name))}
    except Exception as e:
        return {'error': str(e)}

def get_pio_path(p):
    """Return UUID of a path-PIO's driving path object, if any."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        ph = vs.GetCustomObjectPath(h)
        return {'path_id': _oid(ph) if ph else None}
    except Exception as e:
        return {'error': str(e)}


# ── Dimensions ───────────────────────────────────────────────────────────────

def create_linear_dimension(p):
    """Create a linear dimension between two points.

    Gotcha: vs.LinearDim's offsetDistance argument is the text offset ALONG the
    dim line, not a perpendicular offset from the measured object. To center
    the text on the dim line (no perpendicular displacement), pass
    zero_text_perp=True — sets OV 43 = 0 then ResetObject.

    dim_type: 771 aligned, 772 horizontal, 773 vertical (VW enum).
    Pass associate_to=<uuid> to bind the dim to an object (AssociateLinearDimension)."""
    prev = _with_layer_class(p)
    try:
        p1 = (float(p.get('x1', 0)), float(p.get('y1', 0)))
        p2 = (float(p.get('x2', 100)), float(p.get('y2', 0)))
        offset = float(p.get('offset', 0))
        dim_type = int(p.get('dim_type', 771))
        arrow = int(p.get('arrow', 770))
        text_flag = int(p.get('text_flag', 0))
        try:
            vs.LinearDim(p1, p2, offset, dim_type, arrow, text_flag, offset)
        except Exception as e:
            return {'error': str(e)}
        h = vs.LNewObj()
        if h and p.get('zero_text_perp'):
            try:
                vs.SetObjectVariableReal(h, 43, 0.0)
                vs.ResetObject(h)
            except Exception: pass
        assoc_oid = p.get('associate_to')
        if h and assoc_oid:
            ah = _h(assoc_oid)
            if ah:
                try: vs.AssociateLinearDimension(h, ah)
                except Exception: pass
        return _newobj_result(p, fallback=h)
    finally:
        _restore(prev)


def create_angular_dimension(p):
    """Angular dim. cx,cy = vertex; (x1,y1) & (x2,y2) = leg endpoints; offset = arc radius."""
    prev = _with_layer_class(p)
    try:
        cx = float(p.get('cx', 0)); cy = float(p.get('cy', 0))
        p1 = (float(p.get('x1', 100)), float(p.get('y1', 0)))
        p2 = (float(p.get('x2', 0)), float(p.get('y2', 100)))
        offset = float(p.get('offset', 50))
        arrow = int(p.get('arrow', 770))
        text_flag = int(p.get('text_flag', 0))
        # VW2026 AngularDim(startPt, endPt, vert1/arcCenter, textOffset,
        # arrow, textFlag, posAngle) — 7 args; center is the 3rd param.
        try:
            vs.AngularDim(p1, p2, (cx, cy), offset, arrow, text_flag, 0.0)
        except Exception as e:
            return {'error': str(e)}
        return _newobj_result(p)
    finally:
        _restore(prev)

def create_circular_dimension(p):
    """Radial/diameter dim. mode 'radius' or 'diameter'."""
    prev = _with_layer_class(p)
    try:
        cx = float(p.get('cx', 0)); cy = float(p.get('cy', 0))
        tip = (float(p.get('x', 100)), float(p.get('y', 0)))
        mode = p.get('mode', 'radius')
        arrow = int(p.get('arrow', 770))
        text_flag = int(p.get('text_flag', 0))
        try:
            is_diam = 1 if mode == 'diameter' else 0
            vs.CircularDim((cx, cy), tip, is_diam, arrow, text_flag)
        except Exception as e:
            return {'error': str(e)}
        return _newobj_result(p)
    finally:
        _restore(prev)

def create_chain_dimension(p):
    """Chain of linear dimensions through a list of points along the same axis."""
    prev = _with_layer_class(p)
    try:
        pts = p.get('points', [])
        if len(pts) < 2: return {'error': 'need 2+ points'}
        offset = float(p.get('offset', 0))
        dim_type = int(p.get('dim_type', 771))
        arrow = int(p.get('arrow', 770))
        text_flag = int(p.get('text_flag', 0))
        try:
            tpts = [(float(x), float(y)) for x, y in pts]
            vs.CreateChainDimension(tpts, offset, dim_type, arrow, text_flag)
        except AttributeError:
            # Fallback: build pairwise LinearDims
            created = 0
            for i in range(len(pts) - 1):
                a = (float(pts[i][0]), float(pts[i][1]))
                b = (float(pts[i+1][0]), float(pts[i+1][1]))
                try:
                    vs.LinearDim(a, b, offset, dim_type, arrow, text_flag, offset)
                    created += 1
                except Exception: pass
            return {'status': 'ok', 'method': 'fallback', 'created': created}
        except Exception as e:
            return {'error': str(e)}
        return {'status': 'ok'}
    finally:
        _restore(prev)

def associate_linear_dim(p):
    h = _h(p.get('dim_id'))
    ah = _h(p.get('object_id'))
    if not h or not ah: return {'error': 'dim_id and object_id required'}
    try:
        vs.AssociateLinearDimension(h, ah)
        return {'status': 'ok'}
    except Exception as e:
        return {'error': str(e)}

def get_dim_text(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try: return {'text': vs.GetDimText(h)}
    except Exception as e: return {'error': str(e)}

def set_dim_text(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        vs.SetDimText(h, str(p.get('text', '')))
        vs.ResetObject(h)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}

def set_dim_note(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        vs.SetDimNote(h, str(p.get('note', '')))
        vs.ResetObject(h)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}


# ── Site Model (DTM6_*) ──────────────────────────────────────────────────────

def _active_dtm():
    try:
        lay = vs.ActLayer()
        return vs.DTM6_GetDTMObject(lay, True)
    except Exception:
        return None

def send_to_surface(p):
    """Drape a 2D object onto the site-model surface.

    Gotcha: DTM6_SendToSurface converts the 2D input to a 3D polygon and
    vs.LNewObj() does NOT point at the result. Use PrevObj(LNewObj()) when the
    LNewObj handle still matches the input UUID.

    tin_type: 0 existing / 1 proposed / 2 current (default)."""
    oid = p.get('object_id')
    h = _h(oid)
    if not h: return {'error': 'Object not found'}
    tin_type = int(p.get('tin_type', 2))
    dtm_oid = p.get('site_model_id')
    dtm_h = _h(dtm_oid) if dtm_oid else _active_dtm()
    if not dtm_h:
        return {'error': 'No site model on active layer (pass site_model_id)'}
    try:
        ok = vs.DTM6_SendToSurface(dtm_h, h, tin_type)
    except Exception as e:
        return {'error': str(e)}
    new_h = None
    try:
        ln = vs.LNewObj()
        if ln:
            try:
                same = (vs.GetObjectUuid(ln) == oid)
            except Exception:
                same = False
            new_h = vs.PrevObj(ln) if same else ln
    except Exception: pass
    return {'status': 'ok' if ok else 'failed',
            'object_id': _oid(new_h) if new_h else None}

def rise_to_surface(p):
    """Raise an object to the site-model surface (opposite of send_to_surface).
    Same LNewObj gotcha applies — see send_to_surface."""
    oid = p.get('object_id')
    h = _h(oid)
    if not h: return {'error': 'Object not found'}
    tin_type = int(p.get('tin_type', 2))
    dtm_oid = p.get('site_model_id')
    dtm_h = _h(dtm_oid) if dtm_oid else _active_dtm()
    if not dtm_h:
        return {'error': 'No site model on active layer (pass site_model_id)'}
    try:
        ok = vs.DTM6_RiseToSurface(dtm_h, h, tin_type)
    except Exception as e:
        return {'error': str(e)}
    new_h = None
    try:
        ln = vs.LNewObj()
        if ln:
            try:
                same = (vs.GetObjectUuid(ln) == oid)
            except Exception:
                same = False
            new_h = vs.PrevObj(ln) if same else ln
    except Exception: pass
    return {'status': 'ok' if ok else 'failed',
            'object_id': _oid(new_h) if new_h else None}

def get_z_at_xy(p):
    """Z elevation at a planar (x, y) on the site model.
    tin_type: 0 existing / 1 proposed / 2 current (default)."""
    dtm_oid = p.get('site_model_id')
    dtm_h = _h(dtm_oid) if dtm_oid else _active_dtm()
    if not dtm_h:
        return {'error': 'No site model on active layer (pass site_model_id)'}
    x = float(p.get('x', 0)); y = float(p.get('y', 0))
    tin_type = int(p.get('tin_type', 2))
    try:
        ok, z = vs.DTM6_GetZatXY(dtm_h, x, y, tin_type)
        return {'ok': bool(ok), 'z': z if ok else None}
    except Exception as e:
        return {'error': str(e)}

def site_model_on_layer(p):
    layer = p.get('layer')
    try:
        lay_h = vs.GetLayerByName(layer) if layer else vs.ActLayer()
        dtm = vs.DTM6_GetDTMObject(lay_h, True)
        return {'object_id': _oid(dtm) if dtm else None}
    except Exception as e:
        return {'error': str(e)}

def is_site_model(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try: return {'is_site_model': bool(vs.DTM6_IsDTM6Object(h))}
    except Exception as e: return {'error': str(e)}

def clear_site_model_cache(p):
    dtm_oid = p.get('site_model_id')
    dtm_h = _h(dtm_oid) if dtm_oid else _active_dtm()
    if not dtm_h: return {'error': 'site model not found'}
    try:
        vs.DTM6_ClearModelCache(dtm_h)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}

def make_site_modifier_class(p):
    try:
        vs.MakeModifierClass(str(p.get('class_name', '')), int(p.get('modifier_type', 0)))
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}


# ── Hatches / Vector Fills ───────────────────────────────────────────────────

def list_hatches(p):
    try:
        n = vs.NumVectorFills()
        out = []
        for i in range(1, n + 1):
            try: out.append(vs.VectorFillList(i))
            except Exception: pass
        return {'count': n, 'names': out}
    except Exception as e:
        return {'error': str(e)}

def set_hatch_on_object(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    name = p.get('hatch_name')
    if not name: return {'error': 'hatch_name required'}
    try:
        ok = vs.SetVectorFill(h, name)
        return {'status': 'ok' if ok else 'failed'}
    except Exception as e:
        return {'error': str(e)}

def get_hatch_on_object(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try: return {'hatch_name': vs.GetVectorFill(h)}
    except Exception as e: return {'error': str(e)}

def create_static_hatch(p):
    prev = _with_layer_class(p)
    try:
        name = p.get('hatch_name')
        if not name: return {'error': 'hatch_name required'}
        pt = (float(p.get('x', 0)), float(p.get('y', 0)))
        angle = float(p.get('angle', 0))
        try:
            h = vs.CreateStaticHatch(name, pt, angle)
        except Exception as e:
            return {'error': str(e)}
        return _newobj_result(p, fallback=h)
    finally:
        _restore(prev)

def create_static_hatch_from_object(p):
    prev = _with_layer_class(p)
    try:
        src = _h(p.get('source_id'))
        if not src: return {'error': 'source_id not found'}
        name = p.get('hatch_name')
        if not name: return {'error': 'hatch_name required'}
        angle = float(p.get('angle', 0))
        try:
            h = vs.CreateStaticHatchFromObject(src, name, angle)
        except Exception as e:
            return {'error': str(e)}
        return _newobj_result(p, fallback=h)
    finally:
        _restore(prev)

def delete_hatch_definition(p):
    name = p.get('hatch_name')
    if not name: return {'error': 'hatch_name required'}
    try:
        vs.DelVectorFill(name)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}


# ── Solids ───────────────────────────────────────────────────────────────────

def _solid_op(p, fn_name):
    a = _h(p.get('object_id_a')); b = _h(p.get('object_id_b'))
    if not a or not b: return {'error': 'object_id_a and object_id_b required'}
    fn = getattr(vs, fn_name, None)
    if not fn: return {'error': f'{fn_name} not available'}
    try:
        res = fn(a, b)
        # VW returns (result_code, new_handle) for solid ops
        if isinstance(res, tuple):
            new_h = res[1] if len(res) > 1 else None
        else:
            new_h = res
        return {'status': 'ok', 'object_id': _oid(new_h) if new_h else None}
    except Exception as e:
        return {'error': str(e)}

def solid_add(p):        return _solid_op(p, 'AddSolid')
def solid_subtract(p):   return _solid_op(p, 'SubtractSolid')
def solid_intersect(p):  return _solid_op(p, 'IntersectSolid')

def solid_shell(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    thickness = float(p.get('thickness', 10))
    try:
        nh = vs.CreateShell(h, thickness)
        return {'status': 'ok', 'object_id': _oid(nh) if nh else None}
    except Exception as e:
        return {'error': str(e)}

def solid_to_generic(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        vs.CnvrtToGenericSolid(h)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}

def get_solid_volume(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try: return {'volume': vs.ObjVolume(h)}
    except Exception as e: return {'error': str(e)}

def get_solid_surface_area(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try: return {'surface_area': vs.ObjSurfaceArea(h)}
    except Exception as e: return {'error': str(e)}


# ── Data Tags ────────────────────────────────────────────────────────────────

def create_data_tag(p):
    """Data Tag is a PIO — placed via CreateCustomObjectN('Data Tag', ...)."""
    return create_pio({
        'name': 'Data Tag',
        'x': p.get('x', 0), 'y': p.get('y', 0),
        'rotation': p.get('rotation', 0),
        'layer': p.get('layer'),
        'class': p.get('class'),
        'parameters': p.get('parameters'),
    })

def associate_data_tag(p):
    th = _h(p.get('tag_id')); oh = _h(p.get('target_id'))
    if not th or not oh: return {'error': 'tag_id and target_id required'}
    try:
        ok = vs.DT_AssociateWithObj(th, oh)
        return {'status': 'ok' if ok else 'failed'}
    except Exception as e: return {'error': str(e)}

def reset_all_data_tags(p):
    try:
        vs.DT_ResetAllDataTags()
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}

def update_tagged_tags(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        vs.DT_UpdateTaggedTags(h)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}


# ── Graphic Calculation (landscape gold) ─────────────────────────────────────

def offset_polygon(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    dist = float(p.get('distance', 10))
    try:
        nh = vs.OffsetPoly(h, dist)
        return {'status': 'ok', 'object_id': _oid(nh) if nh else None}
    except Exception as e: return {'error': str(e)}

def _poly_boolean(p, fn_name, a_key='clip_id', b_key='subject_id'):
    a = _h(p.get(a_key)); b = _h(p.get(b_key))
    if not a or not b: return {'error': f'{a_key} and {b_key} required'}
    fn = getattr(vs, fn_name, None)
    if not fn: return {'error': f'{fn_name} not available'}
    try:
        nh = fn(a, b)
        return {'status': 'ok', 'object_id': _oid(nh) if nh else None}
    except Exception as e: return {'error': str(e)}

def clip_polygon(p):     return _poly_boolean(p, 'ClipPolygon')
def subtract_polygon(p): return _poly_boolean(p, 'SubtractPolygon', 'object_id_a', 'object_id_b')

def combine_polygons(p):
    ids = p.get('object_ids', [])
    handles = [_h(i) for i in ids if _h(i)]
    if len(handles) < 2: return {'error': 'need 2+ valid polygons'}
    try:
        # CombinePolygons iteratively merges
        acc = handles[0]
        for h in handles[1:]:
            nh = vs.CombinePolygons(acc, h)
            if nh: acc = nh
        return {'status': 'ok', 'object_id': _oid(acc) if acc else None}
    except Exception as e: return {'error': str(e)}

def polygon_centroid(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        # vs.Centroid returns (ok, x, y) on VW2026
        res = vs.Centroid(h)
        if isinstance(res, tuple):
            if len(res) >= 3:
                ok, x, y = res[0], res[1], res[2]
                return {'ok': bool(ok), 'x': x, 'y': y}
            if len(res) == 2:
                return {'ok': True, 'x': res[0], 'y': res[1]}
        return {'value': res}
    except Exception as e: return {'error': str(e)}

def polygon_perimeter(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try: return {'perimeter': vs.CalcPolySegLen(h)}
    except Exception as e: return {'error': str(e)}

def point_in_polygon(p):
    h = _h(p.get('poly_id'))
    if not h: return {'error': 'poly_id not found'}
    pt = (float(p.get('x', 0)), float(p.get('y', 0)))
    try: return {'inside': bool(vs.PtInPoly(pt, h))}
    except Exception as e: return {'error': str(e)}

def point_along_polygon(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    dist = float(p.get('distance', 0))
    try:
        x, y, seg = vs.PointAlongPoly(h, dist)
        return {'x': x, 'y': y, 'segment': seg}
    except Exception as e: return {'error': str(e)}

def convert_to_polygon(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        nh = vs.ConvertToPolygon(h)
        return {'status': 'ok', 'object_id': _oid(nh) if nh else None}
    except Exception as e: return {'error': str(e)}

def convert_to_polyline(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        nh = vs.ConvertToPolyline(h)
        return {'status': 'ok', 'object_id': _oid(nh) if nh else None}
    except Exception as e: return {'error': str(e)}

def convert_to_nurbs(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        nh = vs.ConvertToNURBS(h)
        return {'status': 'ok', 'object_id': _oid(nh) if nh else None}
    except Exception as e: return {'error': str(e)}

def distance(p):
    # plain math — vs.Distance silently returned 0.0 with tuple args
    import math
    return {'distance': math.hypot(p.get('x2', 0) - p.get('x1', 0),
                                   p.get('y2', 0) - p.get('y1', 0))}

def distance_3d(p):
    import math
    return {'distance': math.sqrt(
        (p.get('x2', 0) - p.get('x1', 0)) ** 2 +
        (p.get('y2', 0) - p.get('y1', 0)) ** 2 +
        (p.get('z2', 0) - p.get('z1', 0)) ** 2)}


# ── Materials ────────────────────────────────────────────────────────────────

def create_material(p):
    name = p.get('name')
    if not name: return {'error': 'name required'}
    simple = bool(p.get('simple', True))
    try:
        h = vs.CreateMaterial(name, simple)
        return {'status': 'ok', 'object_id': _oid(h) if h else None}
    except Exception as e: return {'error': str(e)}

def assign_material(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    mname = p.get('material_name')
    if not mname: return {'error': 'material_name required'}
    try:
        mh = vs.GetObject(mname)
        if not mh: return {'error': 'material not found'}
        vs.SetObjMaterialHandle(h, mh)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}

def get_material_info(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        mh = vs.GetObjMaterialHandle(h)
        name = vs.GetObjMaterialName(h) if hasattr(vs, 'GetObjMaterialName') else (vs.GetName(mh) if mh else None)
        return {
            'material_name': name,
            'area':   _safe(lambda: vs.GetMaterialArea(h)),
            'volume': _safe(lambda: vs.GetMaterialVolume(h)),
            'is_simple': _safe(lambda: vs.IsMaterialSimple(mh)) if mh else None,
        }
    except Exception as e: return {'error': str(e)}

def set_material_texture(p):
    mname = p.get('material_name'); tname = p.get('texture_name')
    if not mname or not tname: return {'error': 'material_name and texture_name required'}
    try:
        mh = vs.GetObject(mname); th = vs.GetObject(tname)
        if not mh or not th: return {'error': 'material or texture not found'}
        vs.SetMaterialTexture(mh, th)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}

def set_material_fill(p):
    mname = p.get('material_name'); fill = p.get('fill_name')
    if not mname or not fill: return {'error': 'material_name and fill_name required'}
    try:
        mh = vs.GetObject(mname)
        if not mh: return {'error': 'material not found'}
        vs.SetMaterialFillStyle(mh, fill)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}


# ── Components (walls/slabs/roofs) ───────────────────────────────────────────

def _component_value(value):
    if isinstance(value, (list, tuple)):
        if not value or (isinstance(value[0], bool) and not value[0]): return None
        return value[1] if isinstance(value[0], bool) and len(value) > 1 else value[-1]
    return value


def _component_snapshot(h):
    try:
        raw_count = vs.GetNumberOfComponents(h)
        count = int(_component_value(raw_count) or 0)
    except Exception as exc:
        return None, str(exc)
    rows = []
    for index in range(1, count + 1):
        material_h = _component_value(_safe(lambda index=index: vs.GetComponentMaterial(h, index)))
        rows.append({
            'index': index,
            'name': str(_safe(lambda index=index: vs.GetComponentName(h, index), '') or ''),
            'width': _component_value(_safe(lambda index=index: vs.GetComponentWidth(h, index))),
            'class': str(_component_value(_safe(lambda index=index:
                                                vs.GetComponentClass(h, index))) or ''),
            'function': _component_value(_safe(lambda index=index:
                                               vs.GetComponentFunction(h, index))),
            'material': str(_safe(lambda material_h=material_h: vs.GetName(material_h), '') or '')
                        if material_h else '',
        })
    return rows, None


def _component_snapshots_equal(first, second, tolerance=1e-6):
    if not isinstance(first, list) or not isinstance(second, list) or len(first) != len(second):
        return False
    for left, right in zip(first, second):
        if set(left) != set(right): return False
        for field in left:
            if field == 'width':
                try:
                    if abs(float(left[field]) - float(right[field])) > tolerance: return False
                except Exception:
                    if left[field] != right[field]: return False
            elif left[field] != right[field]:
                return False
    return True

def list_components(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    comps, error = _component_snapshot(h)
    if error: return {'error': error}
    for row in comps:
        index = row['index']
        row['net_area'] = _component_value(_safe(lambda: vs.GetComponentNetArea(h, index)))
        row['net_volume'] = _component_value(_safe(lambda: vs.GetComponentNetVolume(h, index)))
    return {'count': len(comps), 'components': comps}


def _plan_component_edits(p):
    object_id = str(p.get('object_id') or '').strip()
    h = _h(object_id)
    if not h: return None, 'object_id not found'
    updates = p.get('updates')
    if not isinstance(updates, list) or not updates or len(updates) > 100:
        return None, 'updates must be a non-empty list of at most 100 items'
    before, error = _component_snapshot(h)
    if error: return None, error
    if not before: return None, 'object has no editable components'
    allowed = {'name', 'width', 'class', 'function', 'material'}
    normalized, seen = [], set()
    class_states = _class_state_map()
    target = json.loads(json.dumps(before))
    materials = {}
    rollback_materials = {}
    for row in before:
        material = row['material']
        if not material: continue
        material_h = _safe(lambda material=material: vs.GetObject(material))
        if not material_h:
            return None, 'cannot resolve existing component material for safe rollback: %s' % material
        rollback_materials[row['index']] = material_h
    for position, raw in enumerate(updates):
        if not isinstance(raw, dict) or set(raw) - {'index', 'set'}:
            return None, 'updates[%d] supports only index/set' % position
        try: index = int(raw.get('index'))
        except Exception: return None, 'updates[%d].index must be an integer' % position
        values = raw.get('set')
        if index in seen or not 1 <= index <= len(before):
            return None, 'component indexes must be unique and within 1..%d' % len(before)
        if not isinstance(values, dict) or not values or set(values) - allowed:
            return None, 'component set must contain only name/width/class/function/material'
        clean = {}
        if 'name' in values:
            name = str(values['name']).strip()
            if not name or len(name) > 255: return None, 'component name must be 1..255 characters'
            clean['name'] = name
        if 'width' in values:
            try: width = float(values['width'])
            except Exception: return None, 'component width must be numeric'
            if not math.isfinite(width) or width <= 0: return None, 'component width must be finite and positive'
            clean['width'] = width
        if 'class' in values:
            cls = str(values['class']).strip()
            if not cls or cls not in class_states: return None, 'component class not found: %s' % cls
            clean['class'] = cls
        if 'function' in values:
            try: clean['function'] = int(values['function'])
            except Exception: return None, 'component function must be an integer'
        if 'material' in values:
            material = str(values['material']).strip()
            material_h = _safe(lambda material=material: vs.GetObject(material)) if material else None
            if material and not material_h: return None, 'component material not found: %s' % material
            clean['material'] = (str(_safe(lambda: vs.GetName(material_h), '') or material)
                                 if material_h else '')
            materials[(index, material)] = material_h
            if clean['material'] != material:
                materials[(index, clean['material'])] = material_h
        normalized.append({'index': index, 'set': clean})
        target[index - 1].update(clean); seen.add(index)
    payload = {'schema': 1, 'document': _safe(lambda: vs.GetFPathName()) or '',
               'object_id': object_id, 'object_type': int(_safe(lambda: vs.GetTypeN(h), 0) or 0),
               'before': before, 'updates': normalized, 'target': target}
    return {**payload, 'status': 'dry_run', 'read_only': True,
            'summary': {'component_count': len(before), 'update_count': len(normalized),
                        'converged': before == target},
            'plan_token': _revision_digest(payload), '_material_handles': materials,
            '_rollback_material_handles': rollback_materials}, None


def edit_components(p):
    """Token-gated semantic edits for existing wall/slab/roof components."""
    plan, error = _plan_component_edits(p)
    if error: return {'error': error}
    public_plan = {key: value for key, value in plan.items() if not key.startswith('_')}
    if not bool(p.get('confirm', False)): return public_plan
    if str(p.get('plan_token') or '') != plan['plan_token']:
        return {**public_plan, 'status': 'blocked', 'error': 'plan_token missing or stale'}
    if plan['summary']['converged']:
        return {**public_plan, 'status': 'ok', 'changed': False}
    h = _h(plan['object_id'])
    if not h: return {'error': 'object disappeared before apply'}
    selected_before, selection_error = _selected_object_ids()
    if selection_error: return {'error': 'could not snapshot selection: %s' % selection_error}
    setters = {
        'name': vs.SetComponentName, 'width': vs.SetComponentWidth,
        'class': vs.SetComponentClass, 'function': vs.SetComponentFunction,
        'material': vs.SetComponentMaterial,
    }
    applied = []
    _safe(lambda: vs.NameUndoEvent('Agent: edit architectural components'))
    try:
        for update in plan['updates']:
            index = update['index']
            for field in ('name', 'width', 'class', 'function', 'material'):
                if field not in update['set']: continue
                value = update['set'][field]
                api_value = (plan['_material_handles'].get((index, value), 0)
                             if field == 'material' else value)
                if not bool(setters[field](h, index, api_value)):
                    raise RuntimeError('SetComponent%s failed at index %d' %
                                       (field.title(), index))
                applied.append((index, field, plan['before'][index - 1][field]))
        vs.ResetObject(h)
        actual, read_error = _component_snapshot(h)
        if read_error or not _component_snapshots_equal(actual, plan['target']):
            raise RuntimeError(read_error or 'component readback mismatch')
        selection = _restore_selection_ids(selected_before)
        if not selection.get('restored'):
            return {'status': 'partial', 'changed': True,
                    'error': 'components converged but selection restoration failed',
                    'selection': selection, 'verification': actual}
        return {'status': 'ok', 'changed': True, 'plan_token': plan['plan_token'],
                'components': actual, 'selection': selection}
    except Exception as exc:
        rollback_errors = []
        for index, field, value in reversed(applied):
            try:
                api_value = (plan['_rollback_material_handles'].get(index, 0)
                             if field == 'material' else value)
                if not bool(setters[field](h, index, api_value)):
                    raise RuntimeError('setter returned false')
            except Exception as rollback_exc:
                rollback_errors.append({'index': index, 'field': field,
                                        'error': str(rollback_exc)})
        _safe(lambda: vs.ResetObject(h))
        restored, restore_error = _component_snapshot(h)
        rollback_clean = (not rollback_errors and not restore_error and
                          _component_snapshots_equal(restored, plan['before']))
        selection = _restore_selection_ids(selected_before)
        return {'status': 'error', 'error': str(exc), 'rollback_clean': rollback_clean,
                'rollback_errors': rollback_errors, 'selection': selection}

def insert_component(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    before = int(p.get('before_index', 1))
    width = float(p.get('width', 10))
    fill = int(p.get('fill', 1))
    lpw = int(p.get('left_pen_weight', 25))
    rpw = int(p.get('right_pen_weight', 25))
    lps = int(p.get('left_pen_style', 2))
    rps = int(p.get('right_pen_style', 2))
    try:
        ok = vs.InsertNewComponentN(h, before, width, fill, lpw, rpw, lps, rps)
        try: vs.ResetObject(h)
        except Exception: pass
        return {'status': 'ok' if ok else 'failed'}
    except Exception as e:
        return {'error': str(e)}

def delete_component(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    idx = int(p.get('index', 1))
    try:
        vs.DeleteComponent(h, idx)
        vs.ResetObject(h)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}

def delete_all_components(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    try:
        vs.DeleteAllComponents(h)
        vs.ResetObject(h)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}

def _set_component_attr(p, fn_name):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    idx = int(p.get('index', 1))
    fn = getattr(vs, fn_name, None)
    if not fn: return {'error': f'{fn_name} not available'}
    try:
        fn(h, idx, p.get('value'))
        vs.ResetObject(h)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}

def set_component_name(p):     return _set_component_attr(p, 'SetComponentName')
def set_component_class(p):    return _set_component_attr(p, 'SetComponentClass')
def set_component_width(p):    return _set_component_attr(p, 'SetComponentWidth')
def set_component_function(p): return _set_component_attr(p, 'SetComponentFunction')

def set_component_material(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    idx = int(p.get('index', 1))
    mname = p.get('material_name')
    if not mname: return {'error': 'material_name required'}
    try:
        mh = vs.GetObject(mname)
        if not mh: return {'error': 'material not found'}
        vs.SetComponentMaterial(h, idx, mh)
        vs.ResetObject(h)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}

def set_component_texture(p):
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    idx = int(p.get('index', 1))
    tname = p.get('texture_name')
    if not tname: return {'error': 'texture_name required'}
    try:
        th = vs.GetObject(tname)
        if not th: return {'error': 'texture not found'}
        vs.SetComponentTexture(h, idx, th)
        vs.ResetObject(h)
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}


# ── Viewport Class / Layer Overrides ─────────────────────────────────────────

def _vp_cl_props(h):
    return {
        'fill_back':  _safe(lambda: vs.GetVPClOvrdFillBack(h)),
        'fill_fore':  _safe(lambda: vs.GetVPClOvrdFillFore(h)),
        'fill_style': _safe(lambda: vs.GetVPClOvrdFillStyle(h)),
        'pen_back':   _safe(lambda: vs.GetVPClOvrdPenBack(h)),
        'pen_fore':   _safe(lambda: vs.GetVPClOvrdPenFore(h)),
        'fill_opacity': _safe(lambda: vs.GetVPClOvrdFillOpty(h)),
        'pen_opacity':  _safe(lambda: vs.GetVPClOvrdPenOpty(h)),
    }

def add_vp_class_override(p):
    vp = _h(p.get('viewport_id'))
    if not vp: return {'error': 'viewport_id required'}
    cls = p.get('class_name')
    if not cls: return {'error': 'class_name required'}
    try:
        ovrd = vs.CreateVPClOvrd(vp, cls)
        if not ovrd: return {'error': 'CreateVPClOvrd failed'}
        if p.get('fill_fore_rgb'):
            r, g, b = p['fill_fore_rgb']
            vs.SetVPClOvrdFillFore(ovrd, (_c8(r), _c8(g), _c8(b)))
        if p.get('fill_back_rgb'):
            r, g, b = p['fill_back_rgb']
            vs.SetVPClOvrdFillBack(ovrd, (_c8(r), _c8(g), _c8(b)))
        if p.get('pen_fore_rgb'):
            r, g, b = p['pen_fore_rgb']
            vs.SetVPClOvrdPenFore(ovrd, (_c8(r), _c8(g), _c8(b)))
        if p.get('pen_back_rgb'):
            r, g, b = p['pen_back_rgb']
            vs.SetVPClOvrdPenBack(ovrd, (_c8(r), _c8(g), _c8(b)))
        if p.get('fill_opacity') is not None:
            vs.SetVPClOvrdFillOpty(ovrd, int(p['fill_opacity']))
        if p.get('pen_opacity') is not None:
            vs.SetVPClOvrdPenOpty(ovrd, int(p['pen_opacity']))
        if p.get('fill_style') is not None:
            vs.SetVPClOvrdFillStyle(ovrd, int(p['fill_style']))
        try: vs.UpdateVP(vp)
        except Exception: pass
        return {'status': 'ok', 'override_id': _oid(ovrd)}
    except Exception as e:
        return {'error': str(e)}

def remove_vp_class_override(p):
    vp = _h(p.get('viewport_id')); cls = p.get('class_name')
    if not vp or not cls: return {'error': 'viewport_id and class_name required'}
    try:
        vs.RemoveVPClOvrd(vp, cls)
        try: vs.UpdateVP(vp)
        except Exception: pass
        return {'status': 'ok'}
    except Exception as e: return {'error': str(e)}

def list_vp_class_overrides(p):
    vp = _h(p.get('viewport_id'))
    if not vp: return {'error': 'viewport_id required'}
    try:
        n = vs.GetVPClOvrdCount(vp)
    except Exception as e:
        return {'error': str(e)}
    out = []
    # No direct iterator — VW returns handles via a per-index function
    for i in range(1, (n or 0) + 1):
        oh = _safe(lambda i=i: vs.GetVPClOvrdByIndex(vp, i))
        if not oh: continue
        entry = {'index': i, 'class': _safe(lambda: vs.GetVPClOvrdName(oh))}
        entry.update(_vp_cl_props(oh))
        out.append(entry)
    return {'count': n, 'overrides': out}

def add_vp_layer_override(p):
    vp = _h(p.get('viewport_id')); lay = p.get('layer_name')
    if not vp or not lay: return {'error': 'viewport_id and layer_name required'}
    try:
        ovrd = vs.CreateVPLrOvrd(vp, lay)
        if not ovrd: return {'error': 'CreateVPLrOvrd failed'}
        if p.get('fill_fore_rgb'):
            r, g, b = p['fill_fore_rgb']
            vs.SetVPLrOvrdFillFore(ovrd, (_c8(r), _c8(g), _c8(b)))
        if p.get('pen_fore_rgb'):
            r, g, b = p['pen_fore_rgb']
            vs.SetVPLrOvrdPenFore(ovrd, (_c8(r), _c8(g), _c8(b)))
        if p.get('opacity') is not None:
            vs.SetVPLrOvrdOpty(ovrd, int(p['opacity']))
        try: vs.UpdateVP(vp)
        except Exception: pass
        return {'status': 'ok', 'override_id': _oid(ovrd)}
    except Exception as e: return {'error': str(e)}

def list_vp_layer_overrides(p):
    vp = _h(p.get('viewport_id'))
    if not vp: return {'error': 'viewport_id required'}
    try:
        n = vs.GetVPLrOvrdCount(vp)
    except Exception as e:
        return {'error': str(e)}
    out = []
    for i in range(1, (n or 0) + 1):
        oh = _safe(lambda i=i: vs.GetVPLrOvrdByIndex(vp, i))
        if not oh: continue
        out.append({
            'index': i,
            'layer': _safe(lambda: vs.GetVPLrOvrdName(oh)),
            'fill_fore': _safe(lambda: vs.GetVPLrOvrdFillFore(oh)),
            'pen_fore':  _safe(lambda: vs.GetVPLrOvrdPenFore(oh)),
        })
    return {'count': n, 'overrides': out}


# ── Generic Dispatch / Introspection ─────────────────────────────────────────

def list_commands(p):
    """List all callable commands in this module. Optional filter substring.
    Agents use this for discovery when the explicit MCP tool set does not cover
    the verb they need — the full surface is reachable through the `vw`
    dispatcher (or `execute_script` for truly arbitrary vs.* calls)."""
    import inspect
    filt = (p.get('filter') or '').lower()
    out = []
    for name, obj in globals().items():
        if name.startswith('_'): continue
        if not inspect.isfunction(obj): continue
        if filt and filt not in name.lower(): continue
        doc_line = ((obj.__doc__ or '').strip().split('\n')[0])[:140]
        out.append({'name': name, 'doc': doc_line})
    out.sort(key=lambda x: x['name'])
    return {'count': len(out), 'commands': out}

def vs_signature(p):
    """Look up the exact VW2026 signature of a vs.* function from the knowledge
    index (built from the SDK stub). params: {name} or {search, category}.
    Returns args/arity/required/return-type/category/doc — so scripts call
    vs.* correctly the first time instead of triggering engine errors."""
    name = p.get('name')
    if name:
        s = _VS_INDEX.get(name)
        if not s:
            # case-insensitive + prefix fallback
            low = name.lower()
            hits = {k: v for k, v in _VS_INDEX.items() if k.lower() == low}
            if not hits:
                hits = {k: v for k, v in _VS_INDEX.items() if k.lower().startswith(low)}
            if len(hits) == 1:
                name, s = next(iter(hits.items()))
            elif hits:
                return {'ambiguous': sorted(hits)[:40], 'count': len(hits)}
            else:
                return {'error': 'no vs function named %r' % name}
        return {'name': name, **s}
    q = (p.get('search') or '').lower()
    cat = (p.get('category') or '').lower()
    res = []
    for k, v in _VS_INDEX.items():
        if q and q not in k.lower() and q not in v.get('doc', '').lower():
            continue
        if cat and cat not in v.get('cat', '').lower():
            continue
        res.append({'name': k, 'args': v['args'], 'cat': v['cat'], 'doc': v['doc'][:80]})
    res.sort(key=lambda x: x['name'])
    return {'count': len(res), 'functions': res[:200]}

def vs_index_stats(p):
    """Summary of the loaded vs.* knowledge index: total functions + per-category
    counts. Confirms the index is deployed and current."""
    from collections import Counter
    cats = Counter(v.get('cat', '') or '(none)' for v in _VS_INDEX.values())
    return {'functions': len(_VS_INDEX),
            'categories': dict(sorted(cats.items(), key=lambda kv: -kv[1]))}

def _batch_ref_lookup(results, path):
    """Resolve `0.object_id` / `2.items.1.id` against prior batch results."""
    parts = str(path or '').split('.')
    if not parts or not parts[0].isdigit():
        raise ValueError('batch $ref must start with a prior result index')
    index = int(parts.pop(0))
    if index < 0 or index >= len(results):
        raise ValueError('batch $ref result index is not available: %s' % path)
    value = results[index]
    for part in parts:
        if isinstance(value, dict):
            if part not in value:
                raise ValueError('batch $ref key not found: %s' % path)
            value = value[part]
        elif isinstance(value, (list, tuple)) and part.isdigit():
            item_index = int(part)
            if item_index < 0 or item_index >= len(value):
                raise ValueError('batch $ref list index out of range: %s' % path)
            value = value[item_index]
        else:
            raise ValueError('batch $ref cannot traverse %s at %s' % (part, path))
    return value


def _resolve_batch_refs(value, results):
    """Recursively bind exact {"$ref": "N.path"} placeholders."""
    if isinstance(value, dict):
        if set(value.keys()) == {'$ref'}:
            return _batch_ref_lookup(results, value['$ref'])
        return {k: _resolve_batch_refs(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_batch_refs(v, results) for v in value]
    if isinstance(value, tuple):
        return tuple(_resolve_batch_refs(v, results) for v in value)
    return value


def _batch(p):
    """Run several commands in one round-trip (all on VW main thread, serial).

    Later params may consume earlier results with an exact placeholder such as
    {"$ref":"0.object_id"}. `fail_fast=true` stops after the first resolution,
    dispatch, or command error. This is orchestration, not an atomic transaction;
    use guarded compound commands when rollback is required.
    """
    results = []
    calls = p.get('calls') or []
    fail_fast = bool(p.get('fail_fast', False))
    stopped = False
    for call in calls:
        name = call.get('command')
        try:
            params = _resolve_batch_refs(call.get('params') or {}, results)
        except Exception as e:
            results.append({'error': str(e), 'stage': 'resolve_refs',
                            'command': name})
            if fail_fast:
                stopped = True
                break
            continue
        fn = globals().get(name)
        if not fn or not callable(fn) or name.startswith('_'):
            results.append({'error': f'unknown command: {name}'})
            if fail_fast:
                stopped = True
                break
            continue
        try:
            result = fn(params)
            results.append(result)
            if fail_fast and isinstance(result, dict) and result.get('error'):
                stopped = True
                break
        except Exception as e:
            results.append({'error': str(e), 'traceback': traceback.format_exc()})
            if fail_fast:
                stopped = True
                break
    return {'count': len(results), 'requested_count': len(calls),
            'stopped_on_error': stopped, 'results': results}


# ── Script Execution ────────────────────────────────────────────────────────

def execute_script(p):
    import io, sys
    code = p.get('code', '')
    ns = {'vs': vs, '__result__': None}
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    err = None
    try:
        exec(code, ns)
    except Exception:
        err = traceback.format_exc()
    finally:
        sys.stdout = old
    result = ns.get('__result__')
    return {
        'output': buf.getvalue(),
        'error':  err,
        'result': result if isinstance(result, (str, int, float, list, dict, bool, type(None)))
                  else str(result)
    }

def run_menu_command(p):
    cmd = p.get('menu_name') or p.get('command') or ''
    vs.DoMenuTextByName(cmd, p.get('version', 0))
    return {'status': 'ok', 'command': cmd}


# ── Marionette network introspection & editing ───────────────────────────────
# Reverse-engineered on VW2026 (2026-07-06). Key facts:
#   * Marionette wrapper = PIO (type 86) with record 'MarionetteObject2D';
#     network lives in a child GROUP (type 11); result geometry = other children.
#   * Node = PIO (type 86) with record 'MarionetteNode'. Param values live in the
#     node's NodeDef_OIPControls JSON field. Lifted (OIP-visible) params are
#     mirrored in the wrapper's NodeDef_OIPControls JSON (varName '<X>_<n>_Lifted').
#   * Ports = type-17 loci children (order = NodeDef_DataPorts JSON order,
#     inputs first). Wires = type-21 polyline children of the SOURCE node in node-
#     local coords — but they are only a VISUALIZATION: the real connection store
#     is private C++ data (deleted wire polylines get redrawn on execution).
#     vs.SetParent into a node PIO returns False, so wires can NOT be created
#     programmatically; rewire in the GUI editor only.
#   * EmitCode field: writable per-node Python override slot (empty on library
#     nodes). Node scripts themselves are stored in an opaque blob (no script
#     API access; vs.Mrntte_ExportNodeScriptInFile is a silent no-op here).
#   * Re-execution trigger: vs.ResetObject/HMove/HScale2D do NOTHING for
#     Marionette. vs.Scale(1.0,1.0) with ONLY the wrapper selected re-executes
#     the network (forum-confirmed). WARNING: execution runs on the VW main
#     thread and re-enters Python — a node error pops a MODAL Marionette report
#     dialog which blocks this bridge until dismissed (and can kill the socket
#     thread, requiring the bridge to be re-run in VW). Prefer marionette_recalc
#     as the LAST call of a batch.

_MRN_NODE = 'MarionetteNode'
_MRN_WRAP = 'MarionetteObject2D'

def _mrn_is_wrapper(h):
    return _safe(lambda: vs.GetRField(h, _MRN_WRAP, 'NodeType'), '') == 'Wrapper'

def _mrn_group(w):
    ch = vs.FIn3D(w)
    while ch:
        if vs.GetTypeN(ch) == 11:
            return ch
        ch = vs.NextObj(ch)
    return None

def _mrn_nodes(w):
    grp = _mrn_group(w)
    out = []
    if not grp: return out
    ch = vs.FIn3D(grp)
    while ch:
        if vs.GetTypeN(ch) == 86 and _safe(lambda: vs.GetRField(ch, _MRN_NODE, 'NodeType')):
            out.append(ch)
        ch = vs.NextObj(ch)
    return out

def _mrn_json(h, rec, field):
    import json as _json
    raw = _safe(lambda: vs.GetRField(h, rec, field), '') or ''
    try: return _json.loads(raw)
    except Exception: return None

def _mrn_ports(nh):
    """Port list from NodeDef_DataPorts JSON + locus positions (same order:
    inputs first, then outputs)."""
    d = _mrn_json(nh, _MRN_NODE, 'NodeDef_DataPorts') or {'data': []}
    ports = [{'name': e.get('name'), 'loc_name': e.get('locName'),
              'input': bool(e.get('input')), 'description': e.get('description')}
             for e in d.get('data', [])]
    loci = []
    k = vs.FIn3D(nh)
    while k:
        if vs.GetTypeN(k) == 17:
            loci.append(_safe(lambda: vs.GetLocPt(k)))
        k = vs.NextObj(k)
    for i, pt in enumerate(loci):
        if i < len(ports) and pt:
            ports[i]['local_xy'] = [round(pt[0], 6), round(pt[1], 6)]
    return ports

def _mrn_node_info(nh, with_wires=False):
    info = {
        'object_id': _oid(nh),
        'node_type': _safe(lambda: vs.GetRField(nh, _MRN_NODE, 'NodeType')),
        'node_name': _safe(lambda: vs.GetRField(nh, _MRN_NODE, 'NodeName')),
        'disabled': _safe(lambda: vs.GetRField(nh, _MRN_NODE, 'DisableNode')),
        'origin': _safe(lambda: list(vs.GetSymLoc(nh))),
        'oip_controls': (_mrn_json(nh, _MRN_NODE, 'NodeDef_OIPControls') or {}).get('data', []),
        'ports': _mrn_ports(nh),
        'emit_code_len': len(_safe(lambda: vs.GetRField(nh, _MRN_NODE, 'EmitCode'), '') or ''),
    }
    if with_wires:
        wires = []
        k = vs.FIn3D(nh)
        while k:
            if vs.GetTypeN(k) == 21:
                nv = _safe(lambda: vs.GetVertNum(k), 0) or 0
                pts = []
                for i in range(nv):
                    v = _safe(lambda: vs.GetPolylineVertex(k, i + 1))
                    if v: pts.append([round(v[0][0], 6), round(v[0][1], 6)])
                wires.append(pts)
            k = vs.NextObj(k)
        # first polyline is the node frame (starts at local 0,0) — callers skip it
        info['polylines'] = wires
    return info

def _mrn_find_wrapper(p):
    h = _h(p.get('object_id'))
    if not h: return None, {'error': 'Object not found'}
    if not _mrn_is_wrapper(h):
        return None, {'error': 'Object is not a Marionette wrapper (no MarionetteObject2D Wrapper record)'}
    return h, None

def marionette_get_network(p):
    """Introspect a Marionette wrapper: lifted OIP params + all nodes with ports,
    values, positions + wire connectivity (resolved geometrically from the wire
    polylines, which mirror the internal connection store).
    params: object_id (wrapper UUID). Optional: with_polylines (bool), tolerance."""
    w, err = _mrn_find_wrapper(p)
    if err: return err
    nodes = [_mrn_node_info(nh, with_wires=True) for nh in _mrn_nodes(w)]
    tol = float(p.get('tolerance', 0.05))
    port_map = []   # (node_idx, port_idx, world_xy, is_input)
    for ni, n in enumerate(nodes):
        o = n.get('origin') or [0, 0]
        for pi, prt in enumerate(n.get('ports', [])):
            if 'local_xy' in prt:
                port_map.append((ni, pi,
                                 [o[0] + prt['local_xy'][0], o[1] + prt['local_xy'][1]],
                                 prt['input']))
    def _nearest(world, want_input):
        best, bd = None, tol
        for ni, pi, xy, is_in in port_map:
            if is_in != want_input: continue
            d = ((xy[0] - world[0]) ** 2 + (xy[1] - world[1]) ** 2) ** 0.5
            if d < bd: best, bd = (ni, pi), d
        return best
    wires = []
    for ni, n in enumerate(nodes):
        o = n.get('origin') or [0, 0]
        pls = n.pop('polylines', [])
        keep = [] if p.get('with_polylines') else None
        for pl in pls:
            if not pl: continue
            if abs(pl[0][0]) < 1e-6 and abs(pl[0][1]) < 1e-6:
                continue   # node frame polyline
            src = _nearest([o[0] + pl[0][0], o[1] + pl[0][1]], False)
            dst = _nearest([o[0] + pl[-1][0], o[1] + pl[-1][1]], True)
            wires.append({
                'from_node': nodes[src[0]]['object_id'] if src else None,
                'from_port': nodes[src[0]]['ports'][src[1]]['name'] if src else None,
                'to_node': nodes[dst[0]]['object_id'] if dst else None,
                'to_port': nodes[dst[0]]['ports'][dst[1]]['name'] if dst else None,
                'owner_node': n['object_id'],
                'resolved': bool(src and dst),
            })
            if keep is not None: keep.append(pl)
        if keep is not None: n['polylines'] = keep
    return {'status': 'ok',
            'wrapper': {'object_id': _oid(w),
                        'label': _safe(lambda: vs.GetRField(w, _MRN_WRAP, '__ObjectLabel')),
                        'lifted_params': (_mrn_json(w, _MRN_WRAP, 'NodeDef_OIPControls') or {}).get('data', [])},
            'nodes': nodes, 'wires': wires}

def marionette_get_node(p):
    """Full detail for one Marionette node incl. EmitCode. params: object_id (node UUID)."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    if not _safe(lambda: vs.GetRField(h, _MRN_NODE, 'NodeType')):
        return {'error': 'Not a Marionette node'}
    info = _mrn_node_info(h, with_wires=True)
    info['emit_code'] = _safe(lambda: vs.GetRField(h, _MRN_NODE, 'EmitCode'), '')
    return {'status': 'ok', 'node': info}

def marionette_set_node_param(p):
    """Set an OIP control value on a node (the node-local source of truth).
    params: object_id (node UUID), value; and either index (int into
    NodeDef_OIPControls data) or var_name. Apply with marionette_recalc."""
    import json as _json
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    d = _mrn_json(h, _MRN_NODE, 'NodeDef_OIPControls')
    if not d: return {'error': 'Node has no OIP controls'}
    tgt = None
    if p.get('var_name'):
        for c in d.get('data', []):
            if c.get('varName') == p['var_name']: tgt = c; break
    else:
        i = int(p.get('index', 0))
        if i < len(d.get('data', [])): tgt = d['data'][i]
    if tgt is None: return {'error': 'Control not found'}
    old = tgt.get('value')
    tgt['value'] = p.get('value')
    vs.SetRField(h, _MRN_NODE, 'NodeDef_OIPControls', _json.dumps(d))
    return {'status': 'ok', 'var_name': tgt.get('varName'), 'old': old, 'new': p.get('value'),
            'note': 'run marionette_recalc on the wrapper to apply'}

def marionette_set_wrapper_param(p):
    """Set a LIFTED param on a Marionette wrapper AND sync the matching inner
    input node (matched by OIP label text == node name). params: object_id
    (wrapper UUID), name (OIP label, e.g. '2 Hoehe Stütze') or var_name
    (e.g. 'Hoehe_St_tze_50_Lifted'), value."""
    import json as _json
    w, err = _mrn_find_wrapper(p)
    if err: return err
    d = _mrn_json(w, _MRN_WRAP, 'NodeDef_OIPControls')
    if not d: return {'error': 'Wrapper has no lifted params'}
    tgt = None
    for c in d.get('data', []):
        if (p.get('var_name') and c.get('varName') == p['var_name']) or \
           (p.get('name') and c.get('text') == p['name']):
            tgt = c; break
    if tgt is None:
        return {'error': 'Lifted param not found',
                'available': [(c.get('text'), c.get('varName')) for c in d.get('data', [])]}
    old = tgt.get('value')
    tgt['value'] = p.get('value')
    vs.SetRField(w, _MRN_WRAP, 'NodeDef_OIPControls', _json.dumps(d))
    synced = None
    for nh in _mrn_nodes(w):
        if _safe(lambda: vs.GetRField(nh, _MRN_NODE, 'NodeName')) == tgt.get('text'):
            nd = _mrn_json(nh, _MRN_NODE, 'NodeDef_OIPControls')
            if nd and nd.get('data'):
                nd['data'][0]['value'] = p.get('value')
                vs.SetRField(nh, _MRN_NODE, 'NodeDef_OIPControls', _json.dumps(nd))
                synced = _oid(nh)
            break
    return {'status': 'ok', 'var_name': tgt.get('varName'), 'old': old,
            'new': p.get('value'), 'synced_node': synced,
            'note': 'run marionette_recalc on the wrapper to apply'}

def marionette_set_node_code(p):
    """Write the EmitCode override of a node (full node script: Params class +
    RunNode). EXPERIMENTAL: keep port variable names/order identical to the
    original node or wiring breaks. params: object_id (node UUID), code."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    code = p.get('code', '')
    vs.SetRField(h, _MRN_NODE, 'EmitCode', code)
    back = _safe(lambda: vs.GetRField(h, _MRN_NODE, 'EmitCode'), '')
    return {'status': 'ok', 'stored_len': len(back or ''), 'match': back == code}

def marionette_set_node_name(p):
    """Rename a node (NodeName drives OIP label + lift matching).
    params: object_id, name."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    vs.SetRField(h, _MRN_NODE, 'NodeName', p.get('name', ''))
    return {'status': 'ok', 'name': p.get('name', '')}

def marionette_recalc(p):
    """Force a Marionette wrapper to re-execute its network via the
    vs.Scale(1,1) trick (ResetObject does NOT work for Marionette).
    WARNING: if a node errors, VW pops a MODAL error dialog that blocks the
    bridge until dismissed (bridge may need re-running). Make this the LAST
    call in a batch. params: object_id (wrapper UUID)."""
    w, err = _mrn_find_wrapper(p)
    if err: return err
    vs.DSelectAll()
    vs.SetSelect(w)
    vs.Scale(1.0, 1.0)
    vs.DSelectAll()
    return {'status': 'ok',
            'traversal_done': _safe(lambda: vs.GetRField(w, _MRN_WRAP, 'TraversalDone'))}

def marionette_duplicate(p):
    """Duplicate a Marionette wrapper (params can then be varied per copy).
    params: object_id, dx, dy, name (optional vs.SetName)."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    d = vs.HDuplicate(h, float(p.get('dx', 0)), float(p.get('dy', 0)))
    if not d: return {'error': 'HDuplicate failed'}
    if p.get('name'): vs.SetName(d, p['name'])
    return {'status': 'ok', 'object_id': _oid(d)}


# ── Resource manager (Zubehör-Manager: folders, symbols, Marionette styles) ──
# Verified on VW2026 (2026-07-06):
#   * Resource names are GLOBAL and shared with object names — a style cannot
#     have the same name as a drawing object ("naming hiccups"), and two
#     folders cannot both be called 'außen'.
#   * Symbol folders = type 92, symbol defs / plugin styles = type 16.
#   * vs.SetParent(resource, folderHandle) MOVES resources between folders
#     (returns True) — this is the reliable way; vs.InsertSymbolInFolder was a
#     silent no-op in our tests.
#   * Folder CREATION via script (BeginFolderN etc.) is unreliable — it worked
#     exactly once (default name 'Bibliotheksordner') and then never again.
#     resource_create_folder probes the known variants and reports honestly;
#     the robust path is one GUI-created folder, then script renames/moves.
#   * Marionette styles clone perfectly: vs.HDuplicate(styleSym, 0, 0) +
#     SetName + edit the wrapper/node record JSONs inside the symdef.
#     Placement (drag from Zubehör) pushes the style wrapper's lifted values
#     into the fresh network.
#   * Deleting a style resource can DELETE bound instances — unbind first
#     (vs.SetPluginStyle(h, '')). Pasting a styled object from another file
#     imports its style resource.

# External-library resource types live-tested on VW2026 / Emmett 2026-08-09.
# Keep this whitelist explicit: materials do not have a BuildResourceList type,
# and unproven style families must not be guessed from historical constants.
_RESOURCE_KINDS = {
    'symbol': 16,
    'hatch': 66,
    'texture': 97,
}
_RESOURCE_IMPORTABLE_KINDS = {'hatch', 'texture'}
_RESOURCE_INDEX_CACHE = globals().get('_RESOURCE_INDEX_CACHE', {})


def _normalize_resource_items(p):
    items = p.get('items') or []
    if not isinstance(items, list) or not items:
        return None, 'items must be a non-empty list'
    if len(items) > int(p.get('max_items', 50)):
        return None, 'item count exceeds max_items'
    rows, seen = [], set()
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            return None, 'item %d must be an object' % index
        kind = str(raw.get('kind') or '').strip().lower().replace('-', '_')
        if kind not in _RESOURCE_KINDS:
            return None, ('unsupported resource kind %r; verified kinds: %s' %
                          (kind, ', '.join(sorted(_RESOURCE_KINDS))))
        name = str(raw.get('name') or '').strip()
        if not name:
            return None, 'item %d requires name' % index
        key = (kind, name.casefold())
        if key in seen:
            return None, 'duplicate requested resource: %s/%s' % (kind, name)
        seen.add(key)
        source_path = raw.get('source_path')
        if source_path is not None:
            source_path = os.path.abspath(os.path.expanduser(str(source_path)))
            if not source_path.lower().endswith('.vwx'):
                return None, 'source_path must be a .vwx file: %s' % source_path
        rows.append({'kind': kind, 'type': _RESOURCE_KINDS[kind],
                     'name': name, 'source_path': source_path})
    return rows, None


def _resource_library_paths(source_path=None, library_roots=None, max_files=700):
    max_files = max(1, min(int(max_files), 2000))
    if source_path:
        if not os.path.isfile(source_path):
            return None, 'source VWX not found: %s' % source_path, False
        return [source_path], None, False
    roots = library_roots or ['/Applications/Vectorworks 2026/Libraries']
    if isinstance(roots, str):
        roots = [roots]
    paths = []
    for raw_root in roots:
        root = os.path.abspath(os.path.expanduser(str(raw_root)))
        if os.path.isfile(root):
            if root.lower().endswith('.vwx'):
                paths.append(root)
            continue
        if not os.path.isdir(root):
            continue
        for base, dirs, files in os.walk(root):
            dirs.sort()
            for filename in sorted(files):
                if filename.lower().endswith('.vwx'):
                    paths.append(os.path.join(base, filename))
                    if len(paths) >= int(max_files):
                        return paths, None, True
    if not paths:
        return None, 'no .vwx library files found in requested roots', False
    return paths, None, False


def _build_resource_list(resource_type, source_path):
    raw = vs.BuildResourceListN(int(resource_type), source_path)
    if isinstance(raw, (tuple, list)):
        list_id = int(raw[0]) if raw else 0
        hinted_count = int(raw[1]) if len(raw) > 1 else None
    else:
        list_id = int(raw or 0)
        hinted_count = None
    if not list_id:
        return None, 0, hinted_count
    return list_id, int(vs.ResourceListSize(list_id)), hinted_count


def _resource_document_state(kind, name):
    h = _safe(lambda: vs.GetObject(name))
    if not h:
        return {'state': 'missing'}
    actual_type = int(_safe(lambda: vs.GetTypeN(h), 0) or 0)
    if actual_type != _RESOURCE_KINDS[kind]:
        return {'state': 'collision', 'actual_type': actual_type,
                'object_id': _oid(h)}
    return {'state': 'existing', 'actual_type': actual_type,
            'object_id': _oid(h)}


def _resource_index_cache_key(kind, paths):
    fingerprints = []
    for path in paths:
        try:
            stat = os.stat(path)
            fingerprints.append((path, int(stat.st_size),
                                 int(getattr(stat, 'st_mtime_ns', stat.st_mtime * 1e9))))
        except Exception:
            fingerprints.append((path, None, None))
    return (kind, tuple(fingerprints))


def _index_resource_paths(kind, paths):
    cache_key = _resource_index_cache_key(kind, paths)
    if cache_key in _RESOURCE_INDEX_CACHE:
        return _RESOURCE_INDEX_CACHE[cache_key]
    rows = []
    for path in paths:
        list_id, count, _hinted = _build_resource_list(_RESOURCE_KINDS[kind], path)
        if not list_id:
            continue
        for index in range(1, count + 1):
            actual = str(_safe(
                lambda list_id=list_id, index=index:
                vs.GetActualNameFromResourceList(list_id, index), '') or '')
            if not actual:
                continue
            rows.append({'kind': kind, 'type': _RESOURCE_KINDS[kind], 'name': actual,
                         'source_path': path, 'index': index})
    _RESOURCE_INDEX_CACHE[cache_key] = rows
    if len(_RESOURCE_INDEX_CACHE) > 12:
        oldest = next(iter(_RESOURCE_INDEX_CACHE))
        if oldest != cache_key:
            _RESOURCE_INDEX_CACHE.pop(oldest, None)
    return rows


def _match_resource_index(name, rows, suggestion_limit=8):
    exact, insensitive, suggestions = [], [], []
    wanted_fold = name.casefold()
    terms = [term for term in wanted_fold.replace('_', ' ').replace('-', ' ').split()
             if len(term) >= 3]
    for row in rows:
        actual = row['name']
        if actual == name:
            exact.append(row)
        elif actual.casefold() == wanted_fold:
            insensitive.append(row)
        elif (len(suggestions) < int(suggestion_limit) and terms and
              all(term in actual.casefold() for term in terms)):
            suggestions.append(row)
    return {'exact': exact, 'case_insensitive': insensitive,
            'suggestions': suggestions}


def _search_resource_paths(kind, name, paths, suggestion_limit=8):
    result = _match_resource_index(
        name, _index_resource_paths(kind, paths), suggestion_limit)
    result['files_scanned'] = len(paths)
    return result


def _resource_source_fingerprint(row):
    path = row.get('source_path')
    try:
        stat = os.stat(path)
        return {'path': path, 'size': int(stat.st_size),
                'mtime_ns': int(getattr(stat, 'st_mtime_ns', stat.st_mtime * 1e9))}
    except Exception:
        return {'path': path, 'missing': True}


def _resource_plan_token(items, plan):
    payload = {
        'document': _safe(lambda: vs.GetFPathName()) or '',
        'items': items,
        'plan': [{k: v for k, v in row.items() if k not in ('list_id',)}
                 for row in plan],
        'sources': [_resource_source_fingerprint(row)
                    for row in plan if row.get('source_path')],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]


def find_library_resources(p):
    """Search VWX libraries without importing anything.

    Params: kind (symbol/hatch/texture), name, optional source_path or
    library_roots, max_files, suggestion_limit. Exact matches are reported
    separately from case-insensitive matches and conservative suggestions.
    """
    kind = str(p.get('kind') or '').strip().lower().replace('-', '_')
    if kind not in _RESOURCE_KINDS:
        return {'error': 'unsupported resource kind; verified kinds: %s' %
                ', '.join(sorted(_RESOURCE_KINDS))}
    name = str(p.get('name') or '').strip()
    if not name:
        return {'error': 'name required'}
    source_path = p.get('source_path')
    if source_path:
        source_path = os.path.abspath(os.path.expanduser(str(source_path)))
    paths, error, truncated = _resource_library_paths(
        source_path, p.get('library_roots'), p.get('max_files', 700))
    if error:
        return {'error': error}
    result = _search_resource_paths(
        kind, name, paths, p.get('suggestion_limit', 8))
    return {'status': 'ok', 'kind': kind, 'name': name,
            'truncated_by_max_files': truncated, **result}


def _plan_ensure_resources(rows, library_roots=None, max_files=700,
                           suggestion_limit=8):
    plan = []
    path_cache = {}
    index_cache = {}
    for row in rows:
        state = _resource_document_state(row['kind'], row['name'])
        out = dict(row)
        out.update(state)
        if state['state'] != 'missing':
            plan.append(out)
            continue
        cache_key = row.get('source_path') or '__default__'
        if cache_key not in path_cache:
            path_cache[cache_key] = _resource_library_paths(
                row.get('source_path'), library_roots, max_files)
        paths, error, truncated = path_cache[cache_key]
        if error:
            out.update({'state': 'unresolved', 'error': error})
            plan.append(out)
            continue
        index_key = (row['kind'], cache_key)
        if index_key not in index_cache:
            index_cache[index_key] = _index_resource_paths(row['kind'], paths)
        found = _match_resource_index(
            row['name'], index_cache[index_key], suggestion_limit)
        found['files_scanned'] = len(paths)
        if len(found['exact']) == 1:
            out.update(found['exact'][0])
            if row['kind'] in _RESOURCE_IMPORTABLE_KINDS:
                out['state'] = 'import'
            else:
                out['state'] = 'manual_required'
                out['reason'] = (
                    'VW2026 symbol import is modal-blocked in the menu-command pump; '
                    'the source was resolved but must be imported in Resource Manager.')
        elif len(found['exact']) > 1:
            out.update({'state': 'ambiguous', 'matches': found['exact']})
        else:
            out.update({'state': 'unresolved',
                        'case_insensitive': found['case_insensitive'],
                        'suggestions': found['suggestions'],
                        'search_truncated': truncated})
        out['files_scanned'] = found['files_scanned']
        plan.append(out)
    return plan


def ensure_resources(p):
    """Ensure named resources exist, importing safe exact matches as needed.

    Verified kinds: symbol, hatch, texture. Source files may be explicit per
    item; otherwise the installed VW2026 library is searched automatically.
    Dry-run first, then confirm with the returned state-bound plan_token.
    Existing same-type resources are retained. Global wrong-type collisions,
    ambiguous exact matches, and fuzzy-only matches fail closed. Hatch and
    texture imports are unattended-safe. Missing symbols resolve to
    manual_required because both tested symbol import APIs open a blocking
    Untitled document in this execution context. No overwrite.
    """
    rows, error = _normalize_resource_items(p)
    if error:
        return {'error': error}
    plan = _plan_ensure_resources(
        rows, p.get('library_roots'), p.get('max_files', 700),
        p.get('suggestion_limit', 8))
    counts = {state: sum(1 for row in plan if row.get('state') == state)
              for state in ('existing', 'import', 'manual_required', 'collision',
                            'ambiguous', 'unresolved')}
    token = _resource_plan_token(rows, plan)
    base = {'status': 'dry_run', 'plan_token': token, 'items': plan,
            'summary': counts, 'verified_kinds': dict(_RESOURCE_KINDS)}
    if not bool(p.get('confirm', False)):
        return base
    if str(p.get('plan_token') or '') != token:
        return {**base, 'status': 'blocked',
                'error': 'plan_token missing or stale; run a fresh dry-run'}
    blocked = [row for row in plan if row.get('state') in
               ('manual_required', 'collision', 'ambiguous', 'unresolved')]
    if blocked:
        return {**base, 'status': 'blocked',
                'error': 'resource plan contains unresolved or unsafe items'}
    imports = [row for row in plan if row.get('state') == 'import']
    if not imports:
        return {'status': 'ok', 'changed': False, 'plan_token': token,
                'items': plan, 'summary': counts}
    result = _apply_resource_import_plan(
        rows, plan, p.get('library_roots'), p.get('max_files', 700),
        p.get('suggestion_limit', 8))
    result['plan_token'] = token
    return result


def _apply_resource_import_plan(rows, plan, library_roots=None, max_files=700,
                                suggestion_limit=8):
    """Apply one already-approved exact resource plan; never re-resolve first."""
    imports = [row for row in plan if row.get('state') == 'import']
    if not imports:
        return {'status': 'ok', 'changed': False, 'imported_count': 0,
                'imported': [], 'verification': plan}
    created = []
    results = []
    try:
        for row in imports:
            if row['kind'] not in _RESOURCE_IMPORTABLE_KINDS:
                raise RuntimeError('resource kind is not safe for unattended import: %s' %
                                   row['kind'])
            list_id, count, _hinted = _build_resource_list(row['type'], row['source_path'])
            if not list_id or not 1 <= int(row['index']) <= count:
                raise RuntimeError('resource list changed for %s' % row['name'])
            actual = str(vs.GetActualNameFromResourceList(list_id, int(row['index'])) or '')
            if actual != row['name']:
                raise RuntimeError('resource list index changed for %s' % row['name'])
            h = vs.ImportResourceToCurrentFile(list_id, int(row['index']))
            if not h:
                raise RuntimeError('ImportResourceToCurrentFile failed for %s' % row['name'])
            created.append((h, row['name'], row['type']))
            read_name = str(_safe(lambda h=h: vs.GetName(h)) or '')
            read_type = int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0)
            if read_name != row['name'] or read_type != row['type']:
                raise RuntimeError('import readback mismatch for %s: %s/type %s' %
                                   (row['name'], read_name, read_type))
            results.append({'name': read_name, 'kind': row['kind'],
                            'type': read_type, 'object_id': _oid(h),
                            'source_path': row['source_path']})
        final = _plan_ensure_resources(
            rows, library_roots, max_files, suggestion_limit)
        if any(row.get('state') != 'existing' for row in final):
            raise RuntimeError('post-import verification did not converge')
        return {'status': 'ok', 'changed': True,
                'imported_count': len(results), 'imported': results,
                'verification': final}
    except Exception as exc:
        rollback_errors = []
        for h, name, resource_type in reversed(created):
            _safe(lambda h=h: vs.DelObject(h))
            leftover = _safe(lambda name=name: vs.GetObject(name))
            if leftover and int(_safe(lambda leftover=leftover: vs.GetTypeN(leftover), 0) or 0) == resource_type:
                rollback_errors.append(name)
        return {'status': 'error', 'error': str(exc),
                'rollback_attempted': len(created),
                'rollback_top_level_clean': not rollback_errors,
                'rollback_errors': rollback_errors,
                'rollback_scope': ('Top-level requested resources only; imported symbols may '
                                   'leave dependent classes or nested resources behind.')}


_STANDARD_UNIT_PRESETS = {
    'inch': {'index': 2, 'units_per_inch': 1.0},
    'feet': {'index': 3, 'units_per_inch': 1.0 / 12.0},
    'mm': {'index': 7, 'units_per_inch': 25.4},
    'cm': {'index': 8, 'units_per_inch': 2.54},
    'm': {'index': 9, 'units_per_inch': 0.0254},
}


def _unit_key(value):
    key = str(value or '').strip().lower()
    return {
        'in': 'inch', 'inches': 'inch', 'inch': 'inch',
        'ft': 'feet', 'foot': 'feet', 'feet': 'feet',
        'millimeter': 'mm', 'millimeters': 'mm', 'mm': 'mm',
        'centimeter': 'cm', 'centimeters': 'cm', 'cm': 'cm',
        'meter': 'm', 'meters': 'm', 'm': 'm',
    }.get(key)


def _current_unit_key():
    raw = _safe(lambda: vs.GetUnits())
    try:
        units_per_inch = abs(float(raw[3]))
    except Exception:
        return None
    for key, spec in _STANDARD_UNIT_PRESETS.items():
        if abs(units_per_inch - spec['units_per_inch']) <= 1e-7:
            return key
    return None


def _normalize_rgb(value, label):
    if value is None:
        return None, None
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None, '%s must be [r,g,b]' % label
    try:
        rgb = [int(channel) for channel in value]
    except Exception:
        return None, '%s channels must be integers' % label
    if any(channel < 0 or channel > 255 for channel in rgb):
        return None, '%s channels must be 0..255' % label
    return rgb, None


def _normalize_project_standard(p):
    standard = p.get('standard')
    if not isinstance(standard, dict) or not standard:
        return None, 'standard must be a non-empty object'
    allowed = {'document', 'layers', 'classes', 'resources', 'saved_views', 'worksheets'}
    unknown = sorted(set(standard) - allowed)
    if unknown:
        return None, 'unsupported standard sections: %s' % ', '.join(unknown)
    out = {'document': {}, 'layers': [], 'classes': [], 'resources': [],
           'saved_views': [], 'worksheets': []}

    document = standard.get('document') or {}
    if not isinstance(document, dict):
        return None, 'document must be an object'
    if document.get('units') is not None:
        units = _unit_key(document['units'])
        if not units:
            return None, 'document.units must be mm/cm/m/inch/feet'
        out['document']['units'] = units

    seen = set()
    for index, raw in enumerate(standard.get('layers') or []):
        if not isinstance(raw, dict):
            return None, 'layers[%d] must be an object' % index
        name = str(raw.get('name') or '').strip()
        if not name or name.casefold() in seen:
            return None, 'layers require unique non-empty names'
        seen.add(name.casefold())
        layer_type = str(raw.get('type') or 'design').strip().lower()
        if layer_type not in ('design', 'sheet'):
            return None, 'layer %s type must be design or sheet' % name
        row = {'name': name, 'type': layer_type}
        if raw.get('scale') is not None:
            try: row['scale'] = float(raw['scale'])
            except Exception: return None, 'layer %s scale must be numeric' % name
            if row['scale'] <= 0: return None, 'layer %s scale must be positive' % name
        if raw.get('visible') is not None:
            if not isinstance(raw['visible'], bool):
                return None, 'layer %s visible must be boolean' % name
            row['visible'] = bool(raw['visible'])
        if raw.get('elevation') is not None or raw.get('thickness') is not None:
            if layer_type != 'design':
                return None, 'sheet layer %s cannot set elevation/thickness' % name
            try:
                row['elevation'] = float(raw.get('elevation', 0.0))
                row['thickness'] = float(raw.get('thickness', 0.0))
            except Exception:
                return None, 'layer %s elevation/thickness must be numeric' % name
        out['layers'].append(row)

    seen = set()
    for index, raw in enumerate(standard.get('classes') or []):
        if not isinstance(raw, dict):
            return None, 'classes[%d] must be an object' % index
        name = str(raw.get('name') or '').strip()
        if not name or name.casefold() in seen:
            return None, 'classes require unique non-empty names'
        seen.add(name.casefold())
        row = {'name': name}
        if raw.get('visible') is not None:
            if not isinstance(raw['visible'], bool):
                return None, 'class %s visible must be boolean' % name
            row['visible'] = raw['visible']
        for source, target in (('fill_rgb', 'fill'), ('pen_rgb', 'pen')):
            rgb, error = _normalize_rgb(raw.get(source), 'class %s %s' % (name, source))
            if error: return None, error
            if rgb is not None: row[target] = rgb
        if raw.get('line_weight') is not None:
            try: row['lineweight'] = float(raw['line_weight'])
            except Exception: return None, 'class %s line_weight must be numeric' % name
            if row['lineweight'] < 0: return None, 'class %s line_weight must be non-negative' % name
        out['classes'].append(row)

    resources, error = _normalize_resource_items({'items': standard.get('resources') or []}) \
        if standard.get('resources') else ([], None)
    if error: return None, error
    out['resources'] = resources

    seen = set()
    for index, raw in enumerate(standard.get('saved_views') or []):
        if not isinstance(raw, dict): return None, 'saved_views[%d] must be an object' % index
        name = str(raw.get('name') or '').strip()
        if not name or name.casefold() in seen:
            return None, 'saved_views require unique non-empty names'
        seen.add(name.casefold())
        for field in ('save_view', 'save_classes', 'save_layers'):
            if field in raw and not isinstance(raw[field], bool):
                return None, 'saved view %s %s must be boolean' % (name, field)
        out['saved_views'].append({
            'name': name,
            'save_view': bool(raw.get('save_view', True)),
            'save_classes': bool(raw.get('save_classes', True)),
            'save_layers': bool(raw.get('save_layers', True)),
        })

    seen = set()
    for index, raw in enumerate(standard.get('worksheets') or []):
        if not isinstance(raw, dict): return None, 'worksheets[%d] must be an object' % index
        name = str(raw.get('name') or '').strip()
        if not name or name.casefold() in seen:
            return None, 'worksheets require unique non-empty names'
        seen.add(name.casefold())
        try:
            rows = int(raw.get('rows', 10)); columns = int(raw.get('columns', 5))
        except Exception:
            return None, 'worksheet %s rows/columns must be integers' % name
        if not 1 <= rows <= 1000 or not 1 <= columns <= 256:
            return None, 'worksheet %s rows/columns out of range' % name
        cells = []
        for cell in raw.get('cells') or []:
            if not isinstance(cell, dict): return None, 'worksheet %s cells must be objects' % name
            try: row_num, col_num = int(cell['row']), int(cell['col'])
            except Exception: return None, 'worksheet %s cell requires integer row/col' % name
            if not 1 <= row_num <= rows or not 1 <= col_num <= columns:
                return None, 'worksheet %s cell outside declared size' % name
            value = str(cell.get('value', ''))
            if value.lstrip().startswith('='):
                return None, ('worksheet %s starter cells must be static text; '
                              'formulas are not accepted') % name
            cells.append({'row': row_num, 'col': col_num,
                          'value': value})
        out['worksheets'].append({'name': name, 'rows': rows,
                                  'columns': columns, 'cells': cells})

    # Layers and named resources share Vectorworks' global object-name table.
    # Saved Views and worksheets also reject names already used there. Reject
    # cross-section duplicates before the plan can approve two individually-safe
    # creates that would collide halfway through apply.
    global_names = {}
    for section in ('layers', 'resources', 'saved_views', 'worksheets'):
        for row in out[section]:
            folded = row['name'].casefold()
            previous = global_names.get(folded)
            if previous:
                return None, 'name %s is requested in both %s and %s' % (
                    row['name'], previous, section)
            global_names[folded] = section
    return out, None


def _worksheet_names():
    result = get_worksheets({})
    return [str(row.get('name') or '') for row in result.get('worksheets') or []]


def _layer_state_map():
    rows = {}
    for row in get_layers({}).get('layers') or []:
        item = dict(row)
        h = _safe(lambda row=row: vs.GetLayerByName(row['name']))
        if h:
            visibility_code = _safe(lambda h=h: vs.GetObjectVariableInt(h, 153))
            if visibility_code is not None:
                item['visibility_code'] = int(visibility_code)
                item['visible'] = visibility_code == 0
            if row.get('type') == 'design':
                elevation = _safe(lambda h=h: vs.GetLayerElevation(h))
                if isinstance(elevation, (tuple, list)) and len(elevation) >= 2:
                    item['elevation'] = float(elevation[0])
                    item['thickness'] = float(elevation[1])
        rows[row['name']] = item
    return rows


def _class_state_map():
    names = _class_names_all()
    styles = get_class_styles({'names': names}).get('classes') or {}
    return {name: styles.get(name, {}) for name in names}


def _plan_project_standard(standard, allow_units_change=False,
                           library_roots=None, max_files=700, suggestion_limit=8):
    sections = {'document': [], 'layers': [], 'classes': [], 'resources': [],
                'saved_views': [], 'worksheets': []}
    desired_units = standard['document'].get('units')
    if desired_units:
        actual_units = _current_unit_key()
        state = ('unreadable' if actual_units is None else
                 'existing' if desired_units == actual_units else
                 'update' if allow_units_change else 'approval_required')
        sections['document'].append({'name': 'units', 'state': state,
                                     'desired': desired_units, 'actual': actual_units})

    layers = _layer_state_map()
    layers_folded = {name.casefold(): (name, value) for name, value in layers.items()}
    for target in standard['layers']:
        matched = layers_folded.get(target['name'].casefold())
        actual_name, actual = matched if matched else (None, None)
        if not actual:
            collision = _safe(lambda target=target: vs.GetObject(target['name']))
            sections['layers'].append({
                'name': target['name'], 'state': 'collision' if collision else 'create',
                'target': target,
                **({'actual_type': int(_safe(lambda collision=collision: vs.GetTypeN(collision), 0) or 0)}
                   if collision else {}),
            })
            continue
        if actual_name != target['name']:
            sections['layers'].append({'name': target['name'], 'state': 'collision',
                                       'target': target, 'actual': actual,
                                       'reason': 'existing layer differs only by case'})
            continue
        if actual.get('type') != target['type']:
            sections['layers'].append({'name': target['name'], 'state': 'collision',
                                       'target': target, 'actual': actual,
                                       'reason': 'existing layer type differs'})
            continue
        differences = []
        unreadable = []
        for field in ('scale', 'visible', 'elevation', 'thickness'):
            if field in target:
                if actual.get(field) is None:
                    unreadable.append(field)
                elif not _values_equal(target[field], actual.get(field), 0.001):
                    differences.append(field)
        sections['layers'].append({'name': target['name'],
                                   'state': ('unreadable' if unreadable else
                                             'update' if differences else 'existing'),
                                   'target': target, 'actual': actual,
                                   'differences': differences,
                                   'unreadable': unreadable})

    classes = _class_state_map()
    classes_folded = {name.casefold(): (name, value) for name, value in classes.items()}
    for target in standard['classes']:
        matched = classes_folded.get(target['name'].casefold())
        actual_name, actual = matched if matched else (None, None)
        if actual is None:
            collision = _safe(lambda target=target: vs.GetObject(target['name']))
            sections['classes'].append({
                'name': target['name'], 'state': 'collision' if collision else 'create',
                'target': target,
                **({'actual_type': int(_safe(lambda collision=collision: vs.GetTypeN(collision), 0) or 0)}
                   if collision else {}),
            })
            continue
        if actual_name != target['name']:
            sections['classes'].append({'name': target['name'], 'state': 'collision',
                                        'target': target, 'actual': actual,
                                        'reason': 'existing class differs only by case'})
            continue
        differences = []
        unreadable = []
        for field in ('fill', 'pen', 'visible'):
            if field in target:
                if actual.get(field) is None:
                    unreadable.append(field)
                elif not _values_equal(target[field], actual.get(field), 0.001):
                    differences.append(field)
        if 'lineweight' in target:
            if actual.get('lineweight') is None:
                unreadable.append('lineweight')
            else:
                actual_mm = float(actual['lineweight']) / 100.0
                if abs(target['lineweight'] - actual_mm) > 0.001:
                    differences.append('lineweight')
        sections['classes'].append({'name': target['name'],
                                    'state': ('unreadable' if unreadable else
                                              'update' if differences else 'existing'),
                                    'target': target, 'actual': actual,
                                    'differences': differences,
                                    'unreadable': unreadable})

    if standard['resources']:
        sections['resources'] = _plan_ensure_resources(
            standard['resources'], library_roots, max_files, suggestion_limit)

    saved_rows = _saved_view_names_strict()
    saved_names = {name.casefold(): name for name in (saved_rows or [])}
    for target in standard['saved_views']:
        if saved_rows is None:
            sections['saved_views'].append({'name': target['name'], 'state': 'unreadable',
                                            'target': target,
                                            'reason': 'saved view list unreadable'})
            continue
        actual_name = saved_names.get(target['name'].casefold())
        state = ('existing' if actual_name == target['name'] else
                 'collision' if actual_name else 'create')
        if state == 'create' and _safe(lambda target=target: vs.GetObject(target['name'])):
            state = 'collision'
        sections['saved_views'].append({'name': target['name'], 'state': state,
                                        'target': target})

    for target in standard['worksheets']:
        h = _safe(lambda target=target: vs.GetObject(target['name']))
        actual_type = int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0) if h else 0
        # Worksheet resources are type 18. Type 94 is a worksheet image placed
        # on the drawing and must not satisfy the starter-worksheet requirement.
        actual_name = str(_safe(lambda h=h: vs.GetName(h), '') or '') \
            if actual_type == 18 else ''
        state = ('existing' if actual_type == 18 and actual_name == target['name'] else
                 'collision' if h else 'create')
        sections['worksheets'].append({'name': target['name'], 'state': state,
                                       'target': target})

    summary = {}
    for rows in sections.values():
        for row in rows:
            state = row.get('state', 'unknown')
            summary[state] = summary.get(state, 0) + 1
    summary['converged'] = all(row.get('state') == 'existing'
                               for rows in sections.values() for row in rows)
    return {'sections': sections, 'summary': summary}


def _project_standard_token(standard, plan, allow_units_change):
    payload = {
        'document': _safe(lambda: vs.GetFPathName()) or '',
        'standard': standard, 'plan': plan,
        'allow_units_change': bool(allow_units_change),
        'resource_sources': [_resource_source_fingerprint(row)
                             for row in plan['sections']['resources']
                             if row.get('source_path')],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]


def ensure_project_standards(p):
    """Converge safe project setup without deleting or renaming document content.

    Supported sections: document.units; layers (create + scale/visibility/elevation);
    classes (create + visibility/fill/pen/line weight); resources (delegates the
    verified resolver); create-only Saved Views; create-only starter worksheets.
    Dry-run first and confirm with its state-bound token. Units changes additionally
    require allow_units_change=true. Layers/classes/views/worksheets are never pruned,
    renamed, or overwritten. Creating a layer makes the final new layer active because
    VW2026 has no safe active-layer restore call in this bridge context; this is reported.
    """
    standard, error = _normalize_project_standard(p)
    if error: return {'error': error}
    try:
        max_files = int(p.get('max_files', 700))
        suggestion_limit = int(p.get('suggestion_limit', 8))
    except Exception:
        return {'error': 'max_files and suggestion_limit must be integers'}
    if not 1 <= max_files <= 5000 or not 0 <= suggestion_limit <= 100:
        return {'error': 'max_files must be 1..5000 and suggestion_limit 0..100'}
    allow_units_change = bool(p.get('allow_units_change', False))
    plan = _plan_project_standard(
        standard, allow_units_change, p.get('library_roots'),
        max_files, suggestion_limit)
    token = _project_standard_token(standard, plan, allow_units_change)
    base = {'status': 'dry_run', 'plan_token': token, 'standard': standard, **plan}
    if not bool(p.get('confirm', False)):
        return base
    if str(p.get('plan_token') or '') != token:
        return {**base, 'status': 'blocked',
                'error': 'plan_token missing or stale; run a fresh dry-run'}
    blocked = [row for rows in plan['sections'].values() for row in rows
               if row.get('state') not in {'existing', 'create', 'update', 'import'}]
    if blocked:
        return {**base, 'status': 'blocked',
                'error': 'project standard contains approval-required or unsafe items'}
    if plan['summary']['converged']:
        return {'status': 'ok', 'changed': False, 'plan_token': token,
                'verification': plan}

    journal = []
    original_active_layer = _safe(lambda: vs.ActLayer())
    original_active_layer_name = _safe(lambda: vs.GetLName(original_active_layer), '')
    original_active_class = _active_class()
    try:
        for action in plan['sections']['document']:
            if action['state'] == 'update':
                old_key = action.get('actual')
                if old_key not in _STANDARD_UNIT_PRESETS:
                    raise RuntimeError('cannot safely restore unknown current units')
                vs.Units(_STANDARD_UNIT_PRESETS[action['desired']]['index'])
                journal.append(('units', old_key))

        for action in plan['sections']['layers']:
            target = action['target']
            if action['state'] == 'create':
                h = vs.CreateLayer(target['name'], 2 if target['type'] == 'sheet' else 1)
                if not h: raise RuntimeError('CreateLayer failed: %s' % target['name'])
                journal.append(('layer_create', h, target['name']))
            elif action['state'] == 'update':
                h = vs.GetLayerByName(target['name'])
                journal.append(('layer_update', h, action['actual'],
                                action['differences']))
            else:
                continue
            if target.get('scale') is not None: vs.SetLScale(h, target['scale'])
            if target.get('visible') is not None:
                vs.SetObjectVariableInt(h, 153, 0 if target['visible'] else -1)
            if 'elevation' in target or 'thickness' in target:
                vs.SetLayerElevation(h, target.get('elevation', 0.0),
                                     target.get('thickness', 0.0))

        for action in plan['sections']['classes']:
            target = action['target']
            if action['state'] == 'create':
                vs.NameClass(target['name'])
                journal.append(('class_create', target['name']))
            elif action['state'] == 'update':
                journal.append(('class_update', target['name'], action['actual'],
                                action['differences']))
            else:
                continue
            if 'fill' in target:
                rgb = tuple(_c8(value) for value in target['fill'])
                vs.SetClFillBack(target['name'], rgb); vs.SetClFillFore(target['name'], rgb)
            if 'pen' in target:
                rgb = tuple(_c8(value) for value in target['pen'])
                vs.SetClPenBack(target['name'], rgb); vs.SetClPenFore(target['name'], rgb)
            if 'lineweight' in target:
                vs.SetClLW(target['name'], int(round(target['lineweight'] * 100)))
            if target.get('visible') is not None:
                (vs.ShowClass if target['visible'] else vs.HideClass)(target['name'])

        # NameClass activates what it creates and can be restored safely. CreateLayer
        # also activates, but VW2026 exposes no safe layer-activation API in this
        # bridge context; the last created layer intentionally remains active.
        if original_active_class:
            vs.NameClass(original_active_class)

        if standard['resources']:
            resource_result = _apply_resource_import_plan(
                standard['resources'], plan['sections']['resources'],
                p.get('library_roots'), max_files, suggestion_limit)
            if resource_result.get('status') != 'ok':
                raise RuntimeError(resource_result.get('error') or
                                   'resource import did not complete')
            for row in resource_result.get('imported') or []:
                h = _h(row.get('object_id'))
                if h: journal.append(('resource_create', h, row['name']))

        for action in plan['sections']['worksheets']:
            if action['state'] != 'create': continue
            target = action['target']
            h = vs.CreateWS(target['name'], target['rows'], target['columns'])
            if not h: raise RuntimeError('CreateWS failed: %s' % target['name'])
            journal.append(('worksheet_create', h, target['name']))
            for cell in target['cells']:
                vs.SetWSCellFormula(h, cell['row'], cell['col'], cell['row'], cell['col'],
                                    cell['value'])

        for action in plan['sections']['saved_views']:
            if action['state'] != 'create': continue
            target = action['target']
            vs.SaveSheet(target['name'], target['save_view'], target['save_classes'],
                         target['save_layers'])
            journal.append(('saved_view_create', target['name']))
            saved_after = _saved_view_names_strict()
            if saved_after is None or target['name'] not in saved_after:
                raise RuntimeError('SaveSheet failed: %s' % target['name'])

        verification = _plan_project_standard(
            standard, allow_units_change, p.get('library_roots'),
            max_files, suggestion_limit)
        if not verification['summary']['converged']:
            remaining = {
                section: [{'name': row.get('name'), 'state': row.get('state'),
                           'differences': row.get('differences'),
                           'reason': row.get('reason')}
                          for row in rows if row.get('state') != 'existing']
                for section, rows in verification['sections'].items()
                if any(row.get('state') != 'existing' for row in rows)
            }
            raise RuntimeError('post-bootstrap verification did not converge: %s' %
                               json.dumps(remaining, sort_keys=True))
        active_layer_after = _safe(lambda: vs.GetLName(vs.ActLayer()), '')
        return {'status': 'ok', 'changed': True, 'plan_token': token,
                'applied_count': len(journal), 'verification': verification,
                'active_layer_before': original_active_layer_name,
                'active_layer_after': active_layer_after,
                'active_layer_changed': active_layer_after != original_active_layer_name}
    except Exception as exc:
        rollback_errors = []
        if original_active_class:
            _safe(lambda: vs.NameClass(original_active_class))
        for entry in reversed(journal):
            kind = entry[0]
            try:
                if kind == 'saved_view_create': vs.VDelete(entry[1])
                elif kind in ('worksheet_create', 'resource_create'):
                    vs.DelObject(entry[1])
                elif kind == 'layer_create':
                    if _safe(lambda: vs.ActLayer()) == entry[1]:
                        raise RuntimeError(
                            'cannot delete the active created layer; VW2026 has no safe '
                            'layer-activation API in this bridge context')
                    vs.DelObject(entry[1])
                    if _safe(lambda: vs.GetLayerByName(entry[2])):
                        raise RuntimeError('created layer still exists after rollback')
                elif kind == 'class_create':
                    vs.DelClass(entry[1])
                    if entry[1] in _class_names_all():
                        raise RuntimeError('created class still exists after rollback')
                elif kind == 'units': vs.Units(_STANDARD_UNIT_PRESETS[entry[1]]['index'])
                elif kind == 'layer_update':
                    h, old, differences = entry[1], entry[2], entry[3]
                    if 'scale' in differences: vs.SetLScale(h, old['scale'])
                    if 'visible' in differences:
                        vs.SetObjectVariableInt(h, 153, old['visibility_code'])
                    if 'elevation' in differences or 'thickness' in differences:
                        vs.SetLayerElevation(h, old.get('elevation', 0.0),
                                             old.get('thickness', 0.0))
                elif kind == 'class_update':
                    name, old, differences = entry[1], entry[2], entry[3]
                    if 'fill' in differences:
                        rgb = tuple(_c8(value) for value in old['fill'])
                        vs.SetClFillBack(name, rgb); vs.SetClFillFore(name, rgb)
                    if 'pen' in differences:
                        rgb = tuple(_c8(value) for value in old['pen'])
                        vs.SetClPenBack(name, rgb); vs.SetClPenFore(name, rgb)
                    if 'lineweight' in differences: vs.SetClLW(name, int(old['lineweight']))
                    if 'visible' in differences:
                        (vs.ShowClass if old['visible'] else vs.HideClass)(name)
            except Exception as rollback_exc:
                rollback_errors.append({'entry': str(entry[:2]), 'error': str(rollback_exc)})
        if original_active_class: _safe(lambda: vs.NameClass(original_active_class))
        return {'status': 'error', 'error': str(exc),
                'rollback_attempted': len(journal),
                'rollback_top_level_clean': not rollback_errors,
                'rollback_errors': rollback_errors,
                'rollback_scope': ('Created setup resources only; nested dependencies from '
                                   'resource imports are outside rollback scope.')}


_DRAWING_VIEW_TYPES = {
    'plan': (6, 7),
    'top': (0, 7),
    'front': (0, 3),
    'back': (0, 4),
    'left': (0, 5),
    'right': (0, 6),
    'bottom': (0, 8),
    'right_iso': (0, 9),
    'left_iso': (0, 10),
    'right_rear_iso': (0, 11),
    'left_rear_iso': (0, 12),
}
_VP_VISIBILITY = {'hidden': -1, 'visible': 0, 'gray': 2, 'grayed': 2}
# VW2026 live proof: 772 is required for a horizontal value, while 773 returns
# zero for a vertical line. Aligned 771 correctly measures vertical/aligned lines.
_DIMENSION_TYPES = {'aligned': 771, 'horizontal': 772, 'vertical': 771}


def _safe_worksheet_criteria(value):
    text = str(value or '').strip()
    if not text or not text.startswith('(') or not text.endswith(')'):
        return False
    compact_upper = ''.join(text.upper().split())
    if any(token in compact_upper for token in
           ('RUNSCRIPT', 'SCRIPT(', 'EXEC(', 'DATABASE(', ';', '\n', '\r')):
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_ .'-=<>!&|()[],*+/")
    if any(char not in allowed for char in text): return False
    depth = 0; quote = None
    for char in text:
        if quote:
            if char == quote: quote = None
            continue
        if char in ("'", '"'): quote = char
        elif char == '(': depth += 1
        elif char == ')':
            depth -= 1
            if depth < 0: return False
    return depth == 0 and quote is None


def _safe_report_formula(value):
    text = str(value or '').strip()
    simple = {'=N', '=C', '=L', '=COUNT', '=AREA', '=PERIM', '=VOLUME'}
    if text.upper() in simple: return True
    # Record-field lookup: ='Record Name'.'Field Name'
    import re
    return bool(re.match(r"^='[^'\r\n]+('\.'[^'\r\n]+')$", text))


def _normalize_visibility_map(value, label):
    if value is None: return {}, None
    if not isinstance(value, dict): return None, '%s must be an object' % label
    out = {}
    for raw_name, raw_visibility in value.items():
        name = str(raw_name or '').strip()
        visibility = _VP_VISIBILITY.get(str(raw_visibility or '').strip().lower())
        if not name or visibility is None:
            return None, '%s entries require name: visible/hidden/gray' % label
        if name.casefold() in (key.casefold() for key in out):
            return None, '%s names must be unique' % label
        out[name] = visibility
    return out, None


def _normalize_drawing_package(p):
    package = p.get('package')
    if not isinstance(package, dict) or not package:
        return None, 'package must be a non-empty object'
    allowed = {'sheets', 'viewports', 'dimensions', 'reports', 'saved_views'}
    unknown = sorted(set(package) - allowed)
    if unknown: return None, 'unsupported package sections: %s' % ', '.join(unknown)
    out = {key: [] for key in ('sheets', 'viewports', 'dimensions',
                               'reports', 'saved_views')}

    seen = set()
    for index, raw in enumerate(package.get('sheets') or []):
        if not isinstance(raw, dict): return None, 'sheets[%d] must be an object' % index
        name = str(raw.get('name') or '').strip()
        if not name or name.casefold() in seen:
            return None, 'sheets require unique non-empty names'
        seen.add(name.casefold())
        row = {'name': name}
        if raw.get('title') is not None: row['title'] = str(raw['title'])
        if raw.get('dpi') is not None:
            try: row['dpi'] = int(raw['dpi'])
            except Exception: return None, 'sheet %s dpi must be an integer' % name
            if not 36 <= row['dpi'] <= 2400:
                return None, 'sheet %s dpi must be 36..2400' % name
        out['sheets'].append(row)

    seen = set()
    for index, raw in enumerate(package.get('viewports') or []):
        if not isinstance(raw, dict): return None, 'viewports[%d] must be an object' % index
        name = str(raw.get('name') or '').strip()
        sheet = str(raw.get('sheet') or '').strip()
        view = str(raw.get('view') or 'plan').strip().lower()
        if not name or name.casefold() in seen:
            return None, 'viewports require unique non-empty names'
        if not sheet: return None, 'viewport %s requires sheet' % name
        if view not in _DRAWING_VIEW_TYPES:
            return None, 'viewport %s view must be one of %s' % (
                name, ', '.join(sorted(_DRAWING_VIEW_TYPES)))
        seen.add(name.casefold())
        try:
            scale = float(raw.get('scale', 48.0)); x = float(raw.get('x', 0.0)); y = float(raw.get('y', 0.0))
        except Exception:
            return None, 'viewport %s scale/x/y must be numeric' % name
        if scale <= 0: return None, 'viewport %s scale must be positive' % name
        layers, error = _normalize_visibility_map(raw.get('layers'),
                                                  'viewport %s layers' % name)
        if error: return None, error
        classes, error = _normalize_visibility_map(raw.get('classes'),
                                                   'viewport %s classes' % name)
        if error: return None, error
        row = {'name': name, 'sheet': sheet, 'view': view, 'scale': scale,
               'x': x, 'y': y, 'layers': layers, 'classes': classes,
               'hide_other_layers': bool(raw.get('hide_other_layers', True)),
               'hide_other_classes': bool(raw.get('hide_other_classes', False)),
               'black_and_white': bool(raw.get('black_and_white', False))}
        for field in ('hide_other_layers', 'hide_other_classes', 'black_and_white'):
            if field in raw and not isinstance(raw[field], bool):
                return None, 'viewport %s %s must be boolean' % (name, field)
        if row['hide_other_layers'] and not row['layers']:
            return None, ('viewport %s must name at least one visible/gray layer '
                          'when hide_other_layers=true') % name
        out['viewports'].append(row)

    seen = set()
    for index, raw in enumerate(package.get('dimensions') or []):
        if not isinstance(raw, dict): return None, 'dimensions[%d] must be an object' % index
        name = str(raw.get('name') or '').strip()
        viewport = str(raw.get('viewport') or '').strip()
        mode = str(raw.get('mode') or 'aligned').strip().lower()
        if not name or name.casefold() in seen:
            return None, 'dimensions require unique non-empty names'
        if not viewport: return None, 'dimension %s requires viewport' % name
        if mode not in _DIMENSION_TYPES:
            return None, 'dimension %s mode must be aligned/horizontal/vertical' % name
        seen.add(name.casefold())
        try:
            row = {'name': name, 'viewport': viewport, 'mode': mode,
                   'x1': float(raw['x1']), 'y1': float(raw['y1']),
                   'x2': float(raw['x2']), 'y2': float(raw['y2']),
                   'offset': float(raw.get('offset', 0.0)),
                   'arrow': int(raw.get('arrow', 770)),
                   'text_flag': int(raw.get('text_flag', 0))}
        except Exception:
            return None, 'dimension %s requires numeric x1/y1/x2/y2/offset' % name
        if mode == 'horizontal' and abs(row['y2'] - row['y1']) > 1e-9:
            return None, 'horizontal dimension %s requires y1 == y2' % name
        if mode == 'vertical' and abs(row['x2'] - row['x1']) > 1e-9:
            return None, 'vertical dimension %s requires x1 == x2' % name
        out['dimensions'].append(row)

    seen = set()
    for index, raw in enumerate(package.get('reports') or []):
        if not isinstance(raw, dict): return None, 'reports[%d] must be an object' % index
        name = str(raw.get('name') or '').strip(); criteria = str(raw.get('criteria') or '').strip()
        columns = raw.get('columns') or []
        if not name or name.casefold() in seen:
            return None, 'reports require unique non-empty names'
        if not _safe_worksheet_criteria(criteria):
            return None, 'report %s requires safe single-line criteria' % name
        if not isinstance(columns, list) or not columns:
            return None, 'report %s requires columns' % name
        seen.add(name.casefold()); normalized_columns = []
        for column in columns:
            if not isinstance(column, dict): return None, 'report %s columns must be objects' % name
            header = str(column.get('header') or '').strip()
            formula = str(column.get('formula') or '').strip()
            if not header or header.startswith('='):
                return None, 'report %s headers must be static text' % name
            if not _safe_report_formula(formula):
                return None, ('report %s formulas must be =N/=C/=L/=COUNT/=AREA/'
                              '=PERIM/=VOLUME or a record-field lookup') % name
            normalized_columns.append({'header': header, 'formula': formula})
        out['reports'].append({'name': name, 'criteria': criteria,
                               'columns': normalized_columns})

    seen = set()
    for index, raw in enumerate(package.get('saved_views') or []):
        if not isinstance(raw, dict): return None, 'saved_views[%d] must be an object' % index
        name = str(raw.get('name') or '').strip()
        if not name or name.casefold() in seen:
            return None, 'saved_views require unique non-empty names'
        seen.add(name.casefold())
        for field in ('save_view', 'save_classes', 'save_layers'):
            if field in raw and not isinstance(raw[field], bool):
                return None, 'saved view %s %s must be boolean' % (name, field)
        out['saved_views'].append({
            'name': name, 'save_view': bool(raw.get('save_view', True)),
            'save_classes': bool(raw.get('save_classes', True)),
            'save_layers': bool(raw.get('save_layers', True))})

    global_names = {}
    for section in ('sheets', 'viewports', 'dimensions', 'reports', 'saved_views'):
        for row in out[section]:
            folded = row['name'].casefold(); previous = global_names.get(folded)
            if previous:
                return None, 'name %s is requested in both %s and %s' % (
                    row['name'], previous, section)
            global_names[folded] = section
    return out, None


def _vp_visibility_value(value):
    if isinstance(value, (tuple, list)):
        if len(value) >= 2 and bool(value[0]): return int(value[-1])
        return None
    return int(value) if isinstance(value, (int, float)) else None


def _viewport_center(h):
    bounds = _safe(lambda: vs.GetBBox(h))
    if not isinstance(bounds, (tuple, list)) or len(bounds) != 2: return None
    try:
        return {'x': (float(bounds[0][0]) + float(bounds[1][0])) / 2.0,
                'y': (float(bounds[0][1]) + float(bounds[1][1])) / 2.0}
    except Exception:
        return None


def _drawing_sheet_map():
    out = {}
    for row in get_layers({}).get('layers') or []:
        if row.get('type') != 'sheet': continue
        h = _safe(lambda row=row: vs.GetLayerByName(row['name']))
        out[row['name']] = {
            **row,
            'title': str(_safe(lambda h=h: vs.GetObjectVariableString(h, 159), '') or ''),
            'dpi': _safe(lambda h=h: vs.GetObjectVariableInt(h, 155)),
        }
    return out


def _drawing_package_visibility(target, all_names, key, hide_key):
    explicit = target[key]
    if target[hide_key]:
        return {name: explicit.get(name, -1) for name in all_names}
    return dict(explicit)


def _saved_view_names_strict():
    try:
        count = int(vs.SheetNum() or 0)
        names = []
        for index in range(1, count + 1):
            name = str(vs.SheetList(index) or '')
            if name: names.append(name)
        return names
    except Exception:
        return None


def _dimension_text_mm(text):
    # GISDimStringToMM treats VW's feet-inch hyphen as subtraction: 1'-4" -> 8".
    # Parse that native display form ourselves; delegate every other unit format.
    import re
    match = re.match(r'^\s*(\d+)\'\s*-?\s*(\d+(?:\.\d+)?)'
                     r'(?:\s+(\d+)\/(\d+))?"\s*$', str(text or ''))
    if match:
        feet = float(match.group(1)); inches = float(match.group(2))
        if match.group(3): inches += float(match.group(3)) / float(match.group(4))
        return (feet * 12.0 + inches) * 25.4
    return _safe(lambda: vs.GISDimStringToMM(str(text or ''), False))


def _dimension_matches(h, vp, target, tolerance):
    if int(_safe(lambda: vs.GetTypeN(h), 0) or 0) != 63: return False
    annotation_group = _safe(lambda: vs.GetVPGroup(vp, 2))
    if not annotation_group: return None
    parent = _safe(lambda: vs.GetParent(h))
    if parent is None: return None
    if parent != annotation_group:
        return False
    p1 = _safe(lambda: vs.Get2DPt(h, 1)); p2 = _safe(lambda: vs.Get2DPt(h, 2))
    if not p1 or not p2: return None
    expected_dx = target['x2'] - target['x1']; expected_dy = target['y2'] - target['y1']
    if target['mode'] == 'horizontal': expected_dy = 0.0
    elif target['mode'] == 'vertical': expected_dx = 0.0
    if abs((float(p2[0]) - float(p1[0])) - expected_dx) > tolerance: return False
    if abs((float(p2[1]) - float(p1[1])) - expected_dy) > tolerance: return False
    actual_offset = _safe(lambda: vs.GetObjectVariableReal(h, 45))
    if actual_offset is None: return None
    if abs(float(actual_offset) - target['offset']) > tolerance: return False
    expected_length = math.hypot(expected_dx, expected_dy)
    if expected_length > tolerance:
        text = str(_safe(lambda: vs.GetDimText(h), '') or '')
        measured_mm = _dimension_text_mm(text)
        units = _safe(lambda: vs.GetUnits())
        if (measured_mm is None or not isinstance(units, (list, tuple)) or
                len(units) <= 3):
            return None
        try: units_per_inch = float(units[3])
        except Exception: return None
        if units_per_inch <= 0: return None
        expected_mm = expected_length / units_per_inch * 25.4
        tolerance_mm = tolerance / units_per_inch * 25.4
        if abs(float(measured_mm) - expected_mm) > max(0.1, tolerance_mm):
            return False
    return True


def _drawing_report_matches(ws, target):
    expected = {(1, index): column['header']
                for index, column in enumerate(target['columns'], 1)}
    expected[(2, 0)] = '=DATABASE(%s)' % target['criteria']
    expected.update({(2, index): column['formula']
                     for index, column in enumerate(target['columns'], 1)})
    for (row, column), value in expected.items():
        actual = str(_safe(lambda row=row, column=column:
                           vs.GetWSCellFormula(ws, row, column), '') or '')
        if actual.strip() != value.strip(): return False
    return True


def _plan_drawing_package(package, tolerance=0.01):
    sections = {key: [] for key in ('sheets', 'viewports', 'dimensions',
                                    'reports', 'saved_views')}
    sheets = _drawing_sheet_map()
    sheets_folded = {name.casefold(): (name, row) for name, row in sheets.items()}
    all_layers = [row['name'] for row in get_layers({}).get('layers') or []
                  if row.get('type') == 'design']
    # Class None cannot be hidden in a viewport. Leave it inherited/visible.
    all_classes = [name for name in _class_names_all() if name != 'None']
    all_layers_set = set(all_layers); all_classes_set = set(all_classes)

    for target in package['sheets']:
        matched = sheets_folded.get(target['name'].casefold())
        actual_name, actual = matched if matched else (None, None)
        if actual is not None:
            differences = []
            if actual_name != target['name']: differences.append('name_case')
            if 'title' in target and target['title'] != actual.get('title'): differences.append('title')
            if 'dpi' in target and int(target['dpi']) != int(actual.get('dpi') or 0): differences.append('dpi')
            state = 'existing' if not differences else 'collision'
            sections['sheets'].append({'name': target['name'], 'state': state,
                                       'target': target, 'actual': actual,
                                       'differences': differences})
        else:
            collision = _safe(lambda target=target: vs.GetObject(target['name']))
            sections['sheets'].append({'name': target['name'],
                                       'state': 'collision' if collision else 'create',
                                       'target': target})

    desired_sheet_names = {row['name'] for row in package['sheets']}
    for target in package['viewports']:
        if target['sheet'] not in desired_sheet_names and target['sheet'] not in sheets:
            sections['viewports'].append({'name': target['name'], 'state': 'unresolved',
                                          'target': target, 'reason': 'sheet not found'})
            continue
        missing_layers = sorted(set(target['layers']) - all_layers_set)
        missing_classes = sorted(set(target['classes']) - all_classes_set)
        if missing_layers or missing_classes:
            sections['viewports'].append({'name': target['name'], 'state': 'unresolved',
                                          'target': target,
                                          'reason': 'missing layers/classes',
                                          'missing_layers': missing_layers,
                                          'missing_classes': missing_classes})
            continue
        h = _safe(lambda target=target: vs.GetObject(target['name']))
        if not h:
            sections['viewports'].append({'name': target['name'], 'state': 'create',
                                          'target': target})
            continue
        if int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0) != 122:
            sections['viewports'].append({'name': target['name'], 'state': 'collision',
                                          'target': target, 'reason': 'name used by non-viewport'})
            continue
        projection, view_type = _DRAWING_VIEW_TYPES[target['view']]
        center = _viewport_center(h); differences = []
        actual_sheet = str(_safe(lambda h=h: vs.GetLName(vs.GetLayer(h)), '') or '')
        checks = {
            'sheet': actual_sheet == target['sheet'],
            'projection': int(_safe(lambda h=h: vs.GetObjectVariableInt(h, 1000), -999)) == projection,
            'view': int(_safe(lambda h=h: vs.GetObjectVariableInt(h, 1007), -999)) == view_type,
            'scale': _values_equal(target['scale'], _safe(lambda h=h: vs.GetObjectVariableReal(h, 1003)), tolerance),
            'position': bool(center) and _values_equal({'x': target['x'], 'y': target['y']}, center, tolerance),
            'black_and_white': bool(_safe(lambda h=h: vs.GetObjectVariableBoolean(h, 1030), False)) == target['black_and_white'],
        }
        layer_visibility = _drawing_package_visibility(
            target, all_layers, 'layers', 'hide_other_layers')
        class_visibility = _drawing_package_visibility(
            target, all_classes, 'classes', 'hide_other_classes')
        checks['layers'] = all(
            _vp_visibility_value(_safe(lambda h=h, name=name:
                vs.GetVPLayerVisibility(h, vs.GetLayerByName(name)))) == visibility
            for name, visibility in layer_visibility.items())
        checks['classes'] = all(
            _vp_visibility_value(_safe(lambda h=h, name=name:
                vs.GetVPClassVisibility(h, name))) == visibility
            for name, visibility in class_visibility.items())
        differences = [name for name, clean in checks.items() if not clean]
        sections['viewports'].append({'name': target['name'],
                                      'state': 'existing' if not differences else 'collision',
                                      'target': target, 'differences': differences,
                                      'actual': {'sheet': actual_sheet, 'center': center}})

    viewport_names = {row['name'] for row in package['viewports']}
    for target in package['dimensions']:
        vp = _safe(lambda target=target: vs.GetObject(target['viewport']))
        if target['viewport'] not in viewport_names and not vp:
            sections['dimensions'].append({'name': target['name'], 'state': 'unresolved',
                                           'target': target, 'reason': 'viewport not found'})
            continue
        h = _safe(lambda target=target: vs.GetObject(target['name']))
        if not h:
            sections['dimensions'].append({'name': target['name'], 'state': 'create',
                                           'target': target})
            continue
        clean = _dimension_matches(h, vp, target, tolerance) if vp else None
        sections['dimensions'].append({'name': target['name'],
                                       'state': ('unreadable' if clean is None else
                                                 'existing' if clean else 'collision'),
                                       'target': target,
                                       'reason': ('dimension readback unavailable' if clean is None else
                                                  None if clean else
                                                  'dimension type/viewport/value differs')})

    for target in package['reports']:
        h = _safe(lambda target=target: vs.GetObject(target['name']))
        if not h:
            sections['reports'].append({'name': target['name'], 'state': 'create',
                                        'target': target})
            continue
        clean = (int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0) == 18 and
                 _drawing_report_matches(h, target))
        sections['reports'].append({'name': target['name'],
                                    'state': 'existing' if clean else 'collision',
                                    'target': target,
                                    'reason': None if clean else 'worksheet content differs'})

    saved_rows = _saved_view_names_strict()
    saved = {name.casefold(): name for name in (saved_rows or [])}
    for target in package['saved_views']:
        if saved_rows is None:
            sections['saved_views'].append({'name': target['name'], 'state': 'unreadable',
                                            'target': target,
                                            'reason': 'saved view list unreadable'})
            continue
        actual_name = saved.get(target['name'].casefold())
        state = ('existing' if actual_name == target['name'] else
                 'collision' if actual_name else 'create')
        if state == 'create' and _safe(lambda target=target: vs.GetObject(target['name'])):
            state = 'collision'
        sections['saved_views'].append({'name': target['name'], 'state': state,
                                        'target': target,
                                        'verification_level': 'name_only_api_limit'})

    summary = {}
    for rows in sections.values():
        for row in rows:
            state = row['state']; summary[state] = summary.get(state, 0) + 1
    summary['converged'] = all(row['state'] == 'existing'
                               for rows in sections.values() for row in rows)
    return {'sections': sections, 'summary': summary}


def _drawing_package_token(package, plan, tolerance):
    payload = {'document': _safe(lambda: vs.GetFPathName()) or '',
               'package': package, 'plan': plan, 'tolerance': tolerance}
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]


def ensure_drawing_package(p):
    """Create-only drawing package compiler for sheets, viewports, dimensions,
    database reports, and Saved Views. Dry-run first; confirm with the returned
    document-state-bound plan_token. Existing named artifacts must match or the
    package fails closed. Nothing is overwritten, renamed, pruned, or exported.

    Viewport views use verified selectors 1000/1007; no quarantined Layer(),
    Projection(), modal dialogs, title-block PIOs, or PDF export calls are used.
    Explicit dimensions are placed in viewport annotations; auto-dimensioning is
    deliberately outside this create-only compiler.
    """
    package, error = _normalize_drawing_package(p)
    if error: return {'error': error}
    try: tolerance = abs(float(p.get('tolerance', 0.01)))
    except Exception: return {'error': 'tolerance must be numeric'}
    if tolerance <= 0: return {'error': 'tolerance must be positive'}
    plan = _plan_drawing_package(package, tolerance)
    token = _drawing_package_token(package, plan, tolerance)
    base = {'status': 'dry_run', 'plan_token': token, 'package': package, **plan}
    if not bool(p.get('confirm', False)): return base
    if str(p.get('plan_token') or '') != token:
        return {**base, 'status': 'blocked',
                'error': 'plan_token missing or stale; run a fresh dry-run'}
    blocked = [row for rows in plan['sections'].values() for row in rows
               if row['state'] not in {'existing', 'create'}]
    if blocked:
        return {**base, 'status': 'blocked',
                'error': 'drawing package contains collisions or unresolved references'}
    if plan['summary']['converged']:
        return {'status': 'ok', 'changed': False, 'plan_token': token,
                'verification': plan}

    journal = []
    active_before = str(_safe(lambda: vs.GetLName(vs.ActLayer()), '') or '')
    try:
        for action in plan['sections']['sheets']:
            if action['state'] != 'create': continue
            target = action['target']; h = vs.CreateLayer(target['name'], 2)
            if not h: raise RuntimeError('CreateLayer failed: %s' % target['name'])
            journal.append(('sheet_create', h, target['name']))
            if 'title' in target: vs.SetObjectVariableString(h, 159, target['title'])
            if 'dpi' in target: vs.SetObjectVariableInt(h, 155, target['dpi'])

        all_layers = [row['name'] for row in get_layers({}).get('layers') or []
                      if row.get('type') == 'design']
        all_classes = [name for name in _class_names_all() if name != 'None']
        for action in plan['sections']['viewports']:
            if action['state'] != 'create': continue
            target = action['target']; sheet = vs.GetLayerByName(target['sheet'])
            if not sheet: raise RuntimeError('sheet missing at apply: %s' % target['sheet'])
            h = vs.CreateVP(sheet)
            if not h: raise RuntimeError('CreateVP failed: %s' % target['name'])
            journal.append(('viewport_create', h, target['name']))
            vs.SetName(h, target['name'])
            if str(_safe(lambda h=h: vs.GetName(h), '') or '') != target['name']:
                raise RuntimeError('viewport naming failed: %s' % target['name'])
            projection, view_type = _DRAWING_VIEW_TYPES[target['view']]
            vs.SetObjectVariableInt(h, 1000, projection)
            vs.SetObjectVariableInt(h, 1007, view_type)
            vs.SetObjectVariableReal(h, 1003, target['scale'])
            vs.SetObjectVariableBoolean(h, 1030, target['black_and_white'])
            for name, visibility in _drawing_package_visibility(
                    target, all_layers, 'layers', 'hide_other_layers').items():
                vs.SetVPLayerVisibility(h, vs.GetLayerByName(name), visibility)
            for name, visibility in _drawing_package_visibility(
                    target, all_classes, 'classes', 'hide_other_classes').items():
                vs.SetVPClassVisibility(h, name, visibility)
            vs.UpdateVP(h)
            center = _viewport_center(h)
            if not center: raise RuntimeError('viewport bounds unreadable: %s' % target['name'])
            vs.HMove(h, target['x'] - center['x'], target['y'] - center['y'])

        for action in plan['sections']['dimensions']:
            if action['state'] != 'create': continue
            target = action['target']; vp = vs.GetObject(target['viewport'])
            if not vp or int(_safe(lambda vp=vp: vs.GetTypeN(vp), 0) or 0) != 122:
                raise RuntimeError('viewport missing/invalid for dimension: %s' % target['name'])
            vs.LinearDim((target['x1'], target['y1']), (target['x2'], target['y2']),
                         target['offset'], _DIMENSION_TYPES[target['mode']],
                         target['arrow'], target['text_flag'], target['offset'])
            h = vs.LNewObj()
            if h: journal.append(('dimension_create', h, target['name']))
            if not h or int(vs.GetTypeN(h)) != 63:
                raise RuntimeError('LinearDim failed: %s' % target['name'])
            vs.SetName(h, target['name'])
            if str(_safe(lambda h=h: vs.GetName(h), '') or '') != target['name']:
                raise RuntimeError('dimension naming failed: %s' % target['name'])
            vs.SetObjectVariableReal(h, 43, 0.0); vs.ResetObject(h)
            if not vs.AddVPAnnotationObject(vp, h):
                raise RuntimeError('AddVPAnnotationObject failed: %s' % target['name'])

        for action in plan['sections']['reports']:
            if action['state'] != 'create': continue
            target = action['target']; ws = vs.CreateWS(target['name'], 2, len(target['columns']))
            if not ws: raise RuntimeError('CreateWS failed: %s' % target['name'])
            journal.append(('report_create', ws, target['name']))
            for index, column in enumerate(target['columns'], 1):
                vs.SetWSCellFormula(ws, 1, index, 1, index, column['header'])
            vs.SetWSCellFormula(ws, 2, 0, 2, 0, '=DATABASE(%s)' % target['criteria'])
            for index, column in enumerate(target['columns'], 1):
                vs.SetWSCellFormula(ws, 2, index, 2, index, column['formula'])
            vs.RecalculateWS(ws)

        for action in plan['sections']['saved_views']:
            if action['state'] != 'create': continue
            target = action['target']
            current_saved = _saved_view_names_strict()
            if current_saved is None or any(name.casefold() == target['name'].casefold()
                                            for name in current_saved):
                raise RuntimeError('saved view state changed before apply: %s' % target['name'])
            vs.SaveSheet(target['name'], target['save_view'], target['save_classes'],
                         target['save_layers'])
            journal.append(('saved_view_create', target['name']))
            saved_after = _saved_view_names_strict()
            if saved_after is None or target['name'] not in saved_after:
                raise RuntimeError('SaveSheet failed: %s' % target['name'])

        verification = _plan_drawing_package(package, tolerance)
        if not verification['summary']['converged']:
            remaining = {section: [{'name': row['name'], 'state': row['state'],
                                    'differences': row.get('differences'),
                                    'reason': row.get('reason')}
                                   for row in rows if row['state'] != 'existing']
                         for section, rows in verification['sections'].items()
                         if any(row['state'] != 'existing' for row in rows)}
            raise RuntimeError('post-package verification did not converge: %s' %
                               json.dumps(remaining, sort_keys=True))
        active_after = str(_safe(lambda: vs.GetLName(vs.ActLayer()), '') or '')
        report_counts = {}
        for target in package['reports']:
            ws = _safe(lambda target=target: vs.GetObject(target['name']))
            count = _safe(lambda ws=ws: vs.GetWSSubrowCount(ws, 2)) if ws else None
            report_counts[target['name']] = (count[0] if isinstance(count, (list, tuple)) else count)
        return {'status': 'ok', 'changed': True, 'plan_token': token,
                'applied_count': len(journal), 'verification': verification,
                'report_subrows': report_counts,
                'active_layer_before': active_before, 'active_layer_after': active_after,
                'active_layer_changed': active_before != active_after,
                'limitations': {
                    'saved_views': 'flags are write-only; existing views verify by exact name',
                    'dimensions': 'explicit geometry only; no automatic dimension discovery',
                    'export': 'publishing/PDF export remains manual because VW opens modals',
                }}
    except Exception as exc:
        rollback_errors = []
        for entry in reversed(journal):
            try:
                if entry[0] == 'saved_view_create':
                    vs.VDelete(entry[1])
                    names = _saved_view_names_strict()
                    if names is None or entry[1] in names:
                        raise RuntimeError('saved view still exists after rollback')
                elif entry[0] in ('report_create', 'dimension_create', 'viewport_create'):
                    if entry[1]: vs.DelObject(entry[1])
                    if _safe(lambda entry=entry: vs.GetObject(entry[2])):
                        raise RuntimeError('%s still exists after rollback' % entry[0])
                elif entry[0] == 'sheet_create':
                    if _safe(lambda: vs.ActLayer()) == entry[1]:
                        raise RuntimeError('cannot delete active created sheet; VW2026 has '
                                           'no safe layer-activation API in this bridge context')
                    vs.DelObject(entry[1])
            except Exception as rollback_exc:
                rollback_errors.append({'entry': str(entry[:2]),
                                        'error': str(rollback_exc)})
        return {'status': 'error', 'error': str(exc),
                'rollback_attempted': len(journal),
                'rollback_top_level_clean': not rollback_errors,
                'rollback_errors': rollback_errors,
                'rollback_scope': ('Created package artifacts only; the final active '
                                   'created sheet cannot be removed automatically.')}


def _normalize_project_compile(p):
    project = p.get('project')
    if not isinstance(project, dict) or not project:
        return None, 'project must be a non-empty object'
    unknown = sorted(set(project) - {'standard', 'geometry', 'package'})
    if unknown:
        return None, 'unsupported project sections: %s' % ', '.join(unknown)
    out = {}
    if 'standard' in project:
        normalized, error = _normalize_project_standard({'standard': project['standard']})
        if error: return None, 'standard: %s' % error
        out['standard'] = {'input': project['standard'], 'normalized': normalized}
    if 'geometry' in project:
        raw = dict(project['geometry']) if isinstance(project['geometry'], dict) else project['geometry']
        if not isinstance(raw, dict): return None, 'geometry must be an object'
        if bool(raw.get('prune', False)):
            return None, 'project compiler forbids geometry prune'
        layer = str(raw.get('layer') or '').strip()
        prefix = str(raw.get('name_prefix') or '').strip()
        if not layer or not prefix: return None, 'geometry requires layer and name_prefix'
        declared_layers = {row['name'].casefold(): row['name'] for row in
                           out.get('standard', {}).get('normalized', {}).get('layers', [])}
        if layer.casefold() in declared_layers:
            layer = declared_layers[layer.casefold()]
            raw['layer'] = layer
        rows, error = _normalize_native_box_specs(raw)
        if error: return None, 'geometry: %s' % error
        if any(not row['name'].startswith(prefix) for row in rows):
            return None, 'geometry names must start with name_prefix'
        out['geometry'] = {'input': raw, 'normalized': rows,
                           'layer': layer, 'name_prefix': prefix}
    if 'package' in project:
        normalized, error = _normalize_drawing_package({'package': project['package']})
        if error: return None, 'package: %s' % error
        out['package'] = {'input': project['package'], 'normalized': normalized}
    return out, None


def _project_compile_options(p):
    try:
        options = {
            'allow_units_change': bool(p.get('allow_units_change', False)),
            'max_files': int(p.get('max_files', 700)),
            'suggestion_limit': int(p.get('suggestion_limit', 8)),
            'tolerance': abs(float(p.get('tolerance', 0.01))),
            'library_roots': p.get('library_roots'),
        }
    except Exception:
        return None, 'invalid compiler options'
    if not 1 <= options['max_files'] <= 5000:
        return None, 'max_files must be 1..5000'
    if not 0 <= options['suggestion_limit'] <= 100:
        return None, 'suggestion_limit must be 0..100'
    if options['tolerance'] <= 0:
        return None, 'tolerance must be positive'
    return options, None


def _project_compile_plan(project, options):
    stages = {}
    if 'standard' in project:
        inner = _plan_project_standard(
            project['standard']['normalized'], options['allow_units_change'],
            options['library_roots'], options['max_files'], options['suggestion_limit'])
        unsafe = any(row.get('state') not in {'existing', 'create', 'update', 'import'}
                     for rows in inner['sections'].values() for row in rows)
        stages['standard'] = {'state': ('blocked' if unsafe else
                                        'converged' if inner['summary']['converged']
                                        else 'ready'), 'plan': inner}

    if 'geometry' in project:
        target = project['geometry']
        if _safe(lambda: vs.GetLayerByName(target['layer'])):
            payload = dict(target['input']); payload.update({'confirm': False, 'prune': False})
            inner = reconcile_verified_native_boxes(payload)
            if inner.get('error'):
                stages['geometry'] = {'state': 'blocked', 'error': inner['error']}
            else:
                actions = inner.get('actions') or {}
                allowed = {'create', 'update', 'keep', 'unmanaged_extra',
                           'replace', 'delete', 'unsupported'}
                unsafe = (bool(set(actions) - allowed) or
                          any(actions.get(key) for key in ('replace', 'delete', 'unsupported')))
                stages['geometry'] = {
                    'state': ('blocked' if unsafe else
                              'converged' if inner['summary']['converged'] else 'ready'),
                    'plan': inner,
                    **({'error': 'project compiler forbids destructive geometry actions'}
                       if unsafe else {}),
                }
        else:
            declared = {row['name'].casefold() for row in
                        project.get('standard', {}).get('normalized', {}).get('layers', [])}
            stages['geometry'] = {
                'state': 'deferred' if target['layer'].casefold() in declared else 'blocked',
                'reason': ('waiting for standard layer creation' if
                           target['layer'].casefold() in declared
                           else 'target layer is missing and not declared by standard'),
            }

    if 'package' in project:
        inner = ensure_drawing_package({'package': project['package']['input'],
                                        'confirm': False,
                                        'tolerance': options['tolerance']})
        if inner.get('error'):
            stages['package'] = {'state': 'blocked', 'error': inner['error']}
        else:
            states = [row['state'] for rows in inner['sections'].values() for row in rows]
            hard = any(state in {'collision', 'unreadable'} for state in states)
            unresolved = any(state == 'unresolved' for state in states)
            prior_pending = any(row['state'] in {'ready', 'deferred'}
                                for row in stages.values())
            stages['package'] = {
                'state': ('blocked' if hard or (unresolved and not prior_pending) else
                          'deferred' if unresolved else
                          'converged' if inner['summary']['converged'] else 'ready'),
                'plan': inner,
                **({'reason': 'waiting for earlier compiler stages'}
                   if unresolved and prior_pending else {}),
            }
    return {'stages': stages,
            'summary': {
                'converged': all(row['state'] == 'converged' for row in stages.values()),
                'blocked': [name for name, row in stages.items() if row['state'] == 'blocked'],
                'deferred': [name for name, row in stages.items() if row['state'] == 'deferred'],
            }}


def _project_compile_token(project, plan, options):
    payload = {'document': _safe(lambda: vs.GetFPathName()) or '',
               'project': project, 'plan': plan, 'options': options}
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]


def compile_vw_project(p):
    """Run standards -> native boxes -> drawing package under one outer token.

    This is a verified saga, not a transaction: each stage re-plans, applies, and
    verifies itself. If a later stage fails, earlier verified stages remain and the
    response says partial. Destructive geometry replace/prune is refused here.
    """
    project, error = _normalize_project_compile(p)
    if error: return {'error': error}
    options, error = _project_compile_options(p)
    if error: return {'error': error}
    plan = _project_compile_plan(project, options)
    token = _project_compile_token(project, plan, options)
    base = {'status': 'dry_run', 'plan_token': token, 'project': project, **plan}
    if not bool(p.get('confirm', False)): return base
    if str(p.get('plan_token') or '') != token:
        return {**base, 'status': 'blocked',
                'error': 'plan_token missing or stale; run a fresh dry-run'}
    if plan['summary']['blocked']:
        return {**base, 'status': 'blocked', 'error': 'project contains blocked stages'}
    if plan['summary']['converged']:
        return {'status': 'ok', 'changed': False, 'plan_token': token,
                'verification': plan}

    applied, results = [], {}
    for stage in ('standard', 'geometry', 'package'):
        if stage not in project: continue
        result, dry = None, None
        try:
            if stage == 'standard':
                payload = {'standard': project[stage]['input'], 'confirm': False,
                           'allow_units_change': options['allow_units_change'],
                           'max_files': options['max_files'],
                           'suggestion_limit': options['suggestion_limit']}
                if options['library_roots']:
                    payload['library_roots'] = options['library_roots']
                dry = ensure_project_standards(payload)
                if dry.get('error') or dry.get('status') == 'blocked':
                    raise RuntimeError(dry.get('error') or 'standards blocked')
                payload.update({'confirm': True, 'plan_token': dry['plan_token']})
                result = ensure_project_standards(payload)
            elif stage == 'geometry':
                payload = dict(project[stage]['input'])
                payload.update({'confirm': False, 'prune': False})
                dry = reconcile_verified_native_boxes(payload)
                if dry.get('error'): raise RuntimeError(dry['error'])
                actions = dry.get('actions') or {}
                allowed = {'create', 'update', 'keep', 'unmanaged_extra',
                           'replace', 'delete', 'unsupported'}
                if set(actions) - allowed:
                    raise RuntimeError('geometry plan contains unknown action types')
                if any(actions.get(key) for key in ('replace', 'delete', 'unsupported')):
                    raise RuntimeError('geometry became destructive before apply')
                payload.update({'confirm': True, 'plan_token': dry['plan_token']})
                result = reconcile_verified_native_boxes(payload)
            else:
                payload = {'package': project[stage]['input'], 'confirm': False,
                           'tolerance': options['tolerance']}
                dry = ensure_drawing_package(payload)
                if dry.get('error') or dry.get('status') == 'blocked':
                    raise RuntimeError(dry.get('error') or 'drawing package blocked')
                payload.update({'confirm': True, 'plan_token': dry['plan_token']})
                result = ensure_drawing_package(payload)
            if result.get('status') != 'ok':
                raise RuntimeError(result.get('error') or '%s did not complete' % stage)
            results[stage] = result
            if result.get('changed'): applied.append(stage)
        except Exception as exc:
            failed = result if isinstance(result, dict) else (
                dry if isinstance(dry, dict) else None)
            dirty = bool(failed and (
                failed.get('rollback_top_level_clean') is False or
                failed.get('destructive_started') is True))
            return {'status': 'partial' if applied or dirty else 'error',
                    'error': str(exc),
                    'failed_stage': stage, 'applied_stages': applied,
                    'stage_results': results,
                    'failed_stage_result': failed,
                    'manual_recovery': (
                        'The failed stage reports an incomplete rollback; inspect its full '
                        'result and use Vectorworks Undo/manual cleanup before re-running.'
                        if dirty else
                        'Earlier verified stages remain; correct the failed stage and re-run '
                        'the same manifest.')}
    verification = _project_compile_plan(project, options)
    if not verification['summary']['converged']:
        return {'status': 'partial', 'error': 'final project verification did not converge',
                'applied_stages': applied, 'stage_results': results,
                'verification': verification}
    return {'status': 'ok', 'changed': bool(applied), 'plan_token': token,
            'applied_stages': applied, 'stage_results': results,
            'verification': verification}


def list_assembly_templates(p):
    return {'status': 'ok', 'read_only': True,
            'templates': _assembly_templates.catalog(),
            'count': len(_assembly_templates.TEMPLATES),
            'contract': ('expand_assembly_template returns project.geometry input; '
                         'compile_vw_project remains the only mutation path')}


def expand_assembly_template(p):
    result, error = _assembly_templates.expand(
        str(p.get('template') or '').strip(), p.get('parameters'),
        p.get('max_items', 500))
    if error: return {'error': error}
    # Reuse the production geometry normalizer as an integration assertion.
    normalized, normalize_error = _normalize_native_box_specs(result['geometry'])
    if normalize_error: return {'error': 'expanded geometry failed validation: %s' % normalize_error}
    return {'status': 'ok', 'read_only': True, **result,
            'normalized_count': len(normalized)}


_AUTO_DIM_PLANES = {
    'plan': ('x', 'y'),
    'front': ('x', 'z'),
    'side': ('y', 'z'),
}


def _auto_dim_slug(value, limit=28):
    cleaned = ''.join(char if char.isalnum() else '-' for char in str(value).upper())
    cleaned = '-'.join(part for part in cleaned.split('-') if part)
    if len(cleaned) <= limit: return cleaned
    digest = hashlib.sha1(cleaned.encode('utf-8')).hexdigest()[:8].upper()
    return '%s-%s' % (cleaned[:limit - 9].rstrip('-'), digest)


def _auto_dim_name(rule, axis, viewport, scope, sources):
    identity = '|'.join([rule, axis, viewport, scope] + sorted(sources))
    digest = hashlib.sha1(identity.encode('utf-8')).hexdigest()[:10].upper()
    stem = 'AD-%s-%s-%s' % (rule.upper(), axis.upper(), _auto_dim_slug(viewport, 18))
    return '%s-%s' % (stem[:52].rstrip('-'), digest)


def _auto_dim_spec(name, viewport, mode, h1, v1, h2, v2, offset,
                   horizontal_axis, sources, rule):
    return {
        'name': name, 'viewport': viewport, 'mode': mode,
        'x1': round(h1, 6), 'y1': round(v1, 6),
        'x2': round(h2, 6), 'y2': round(v2, 6),
        'offset': round(offset, 6),
        '_rule': rule, '_axis': horizontal_axis,
        '_sources': list(sources),
    }


def plan_auto_dimensions(p):
    """Derive deterministic explicit dimension specs from named native geometry.

    Read-only: returns package.dimensions rows for ensure_drawing_package. Scope is
    deliberately narrow (layer + name_prefix), and ambiguous center chains fail
    closed rather than guessing an axis.
    """
    layer = str(p.get('layer') or '').strip()
    prefix = str(p.get('name_prefix') or '').strip()
    viewport = str(p.get('viewport') or '').strip()
    plane = str(p.get('plane') or 'plan').strip().lower()
    if not layer or not prefix or not viewport:
        return {'error': 'layer, name_prefix, and viewport are required'}
    if plane not in _AUTO_DIM_PLANES:
        return {'error': 'plane must be plan, front, or side'}
    rules = p.get('rules') or ['overall']
    if not isinstance(rules, list) or not rules:
        return {'error': 'rules must be a non-empty list'}
    rules = [str(rule).strip().lower() for rule in rules]
    unknown = sorted(set(rules) - {'overall', 'centers', 'members', 'contacts'})
    if unknown: return {'error': 'unsupported dimension rules: %s' % ', '.join(unknown)}
    if len(set(rules)) != len(rules): return {'error': 'dimension rules must be unique'}
    center_axis = str(p.get('center_axis') or '').strip().lower()
    if center_axis and center_axis not in _AUTO_DIM_PLANES[plane]:
        return {'error': 'center_axis must be one of the two visible plane axes'}
    try:
        tolerance = abs(float(p.get('tolerance', 0.01)))
        offset = abs(float(p.get('offset', 6.0)))
        spacing = abs(float(p.get('spacing', 3.0)))
        limit = int(p.get('limit', 100))
    except Exception:
        return {'error': 'tolerance/offset/spacing/limit values are invalid'}
    if tolerance <= 0 or spacing <= 0 or not 1 <= limit <= 500:
        return {'error': 'tolerance/spacing must be positive and limit must be 1..500'}
    vp = _safe(lambda: vs.GetObject(viewport))
    if not vp or int(_safe(lambda: vs.GetTypeN(vp), 0) or 0) != 122:
        return {'error': 'viewport must resolve to an existing viewport'}
    projection = int(_safe(lambda: vs.GetObjectVariableInt(vp, 1000), -999) or -999)
    view_type = int(_safe(lambda: vs.GetObjectVariableInt(vp, 1007), -999) or -999)
    view_clean = ((plane == 'plan' and view_type == 7 and projection in (0, 6)) or
                  (plane == 'front' and view_type == 3) or
                  (plane == 'side' and view_type in (5, 6)))
    if not view_clean:
        return {'error': 'plane does not match the target viewport view'}
    manifest = get_assembly_manifest({
        'layer': layer, 'name_prefix': prefix, 'limit': limit + 1,
        'guard': int(p.get('guard', 60000)),
    })
    if manifest.get('error'): return {'error': manifest['error']}
    if manifest.get('truncated') or int(manifest.get('count') or 0) > limit:
        return {'error': 'scope exceeds limit; narrow name_prefix'}
    rows = manifest.get('objects') or []
    if not rows: return {'error': 'scope matched no geometry'}
    snapshot = []
    for row in rows:
        name = str(row.get('name') or '')
        center, dims = row.get('center'), row.get('dimensions')
        if not name or not isinstance(center, dict) or not isinstance(dims, dict):
            return {'error': 'all scoped objects must be named readable 3D geometry'}
        try:
            bounds = {axis: (float(center[axis]) - float(dims[axis]) / 2.0,
                             float(center[axis]) + float(dims[axis]) / 2.0)
                      for axis in ('x', 'y', 'z')}
        except Exception:
            return {'error': 'unreadable center/dimensions for %s' % name}
        if any(bounds[axis][1] - bounds[axis][0] <= tolerance for axis in ('x', 'y', 'z')):
            return {'error': 'degenerate geometry in scope: %s' % name}
        snapshot.append({'name': name, 'center': {axis: float(center[axis])
                                                 for axis in ('x', 'y', 'z')},
                         'bounds': bounds})
    snapshot.sort(key=lambda row: row['name'])
    h_axis, v_axis = _AUTO_DIM_PLANES[plane]
    mins = {axis: min(row['bounds'][axis][0] for row in snapshot)
            for axis in ('x', 'y', 'z')}
    maxs = {axis: max(row['bounds'][axis][1] for row in snapshot)
            for axis in ('x', 'y', 'z')}
    dimensions, derivations = [], []
    scope_key = '%s|%s' % (layer, prefix)

    def emit(spec):
        derivations.append({'name': spec['name'], 'rule': spec.pop('_rule'),
                            'axis': spec.pop('_axis'), 'sources': spec.pop('_sources')})
        dimensions.append(spec)

    if 'overall' in rules:
        all_sources = [row['name'] for row in snapshot]
        emit(_auto_dim_spec(_auto_dim_name('overall', h_axis, viewport, scope_key,
                                           all_sources), viewport, 'horizontal',
                            mins[h_axis], mins[v_axis],
                            maxs[h_axis], mins[v_axis], offset,
                            h_axis, all_sources, 'overall'))
        emit(_auto_dim_spec(_auto_dim_name('overall', v_axis, viewport, scope_key,
                                           all_sources), viewport, 'vertical',
                            mins[h_axis], mins[v_axis],
                            mins[h_axis], maxs[v_axis], offset,
                            v_axis, all_sources, 'overall'))

    if 'centers' in rules:
        spread_h = max(row['center'][h_axis] for row in snapshot) - \
                   min(row['center'][h_axis] for row in snapshot)
        spread_v = max(row['center'][v_axis] for row in snapshot) - \
                   min(row['center'][v_axis] for row in snapshot)
        if not center_axis and abs(spread_h - spread_v) <= tolerance:
            return {'error': 'center-chain dominant axis is ambiguous'}
        axis = center_axis or (h_axis if spread_h > spread_v else v_axis)
        mode = 'horizontal' if axis == h_axis else 'vertical'
        ordered = sorted(snapshot, key=lambda row: (row['center'][axis], row['name']))
        groups = []
        for row in ordered:
            if not groups or abs(row['center'][axis] - groups[-1][0]) > tolerance:
                groups.append([row['center'][axis], [row['name']]])
            else:
                groups[-1][1].append(row['name'])
        if len(groups) < 2: return {'error': 'center-chain has fewer than two unique centers'}
        baseline = maxs[v_axis] if mode == 'horizontal' else maxs[h_axis]
        for index, (left, right) in enumerate(zip(groups, groups[1:]), 1):
            if mode == 'horizontal':
                sources = left[1] + right[1]
                spec = _auto_dim_spec(_auto_dim_name('centers', axis, viewport,
                                                      scope_key, sources),
                                      viewport, mode, left[0], baseline, right[0], baseline,
                                      offset, axis, sources, 'centers')
            else:
                sources = left[1] + right[1]
                spec = _auto_dim_spec(_auto_dim_name('centers', axis, viewport,
                                                      scope_key, sources),
                                      viewport, mode, baseline, left[0], baseline, right[0],
                                      offset, axis, sources, 'centers')
            emit(spec)

    if 'members' in rules:
        for index, row in enumerate(snapshot, 1):
            for axis, mode in ((h_axis, 'horizontal'), (v_axis, 'vertical')):
                other = v_axis if mode == 'horizontal' else h_axis
                baseline = row['bounds'][other][1]
                line_offset = offset + spacing * (index - 1)
                name = _auto_dim_name('members', axis, viewport, scope_key, [row['name']])
                if mode == 'horizontal':
                    spec = _auto_dim_spec(name, viewport, mode,
                                          row['bounds'][axis][0], baseline,
                                          row['bounds'][axis][1], baseline, line_offset,
                                          axis, [row['name']], 'members')
                else:
                    spec = _auto_dim_spec(name, viewport, mode,
                                          baseline, row['bounds'][axis][0],
                                          baseline, row['bounds'][axis][1], line_offset,
                                          axis, [row['name']], 'members')
                emit(spec)

    if 'contacts' in rules:
        contact_rows = []
        for left_index, left in enumerate(snapshot):
            for right in snapshot[left_index + 1:]:
                touching = []
                for axis in ('x', 'y', 'z'):
                    gap = max(0.0, max(left['bounds'][axis][0], right['bounds'][axis][0]) -
                              min(left['bounds'][axis][1], right['bounds'][axis][1]))
                    if gap <= tolerance:
                        touching.append(axis)
                face_axes = []
                for axis in touching:
                    face_gap = min(abs(left['bounds'][axis][1] - right['bounds'][axis][0]),
                                   abs(right['bounds'][axis][1] - left['bounds'][axis][0]))
                    others_overlap = all(
                        min(left['bounds'][other][1], right['bounds'][other][1]) -
                        max(left['bounds'][other][0], right['bounds'][other][0]) > tolerance
                        for other in ('x', 'y', 'z') if other != axis)
                    if face_gap <= tolerance and others_overlap: face_axes.append(axis)
                visible = [axis for axis in face_axes if axis in (h_axis, v_axis)]
                if len(visible) == 1:
                    contact_rows.append((visible[0], left, right))
        contact_rows.sort(key=lambda row: (row[0], row[1]['name'], row[2]['name']))
        for index, (axis, left, right) in enumerate(contact_rows, 1):
            mode = 'horizontal' if axis == h_axis else 'vertical'
            sources = [left['name'], right['name']]
            name = _auto_dim_name('contact', axis, viewport, scope_key, sources)
            if mode == 'horizontal':
                baseline = max(left['bounds'][v_axis][1], right['bounds'][v_axis][1])
                line_offset = offset + spacing * (index - 1)
                spec = _auto_dim_spec(name,
                                      viewport, mode, left['center'][axis], baseline,
                                      right['center'][axis], baseline, line_offset,
                                      axis, sources, 'contacts')
            else:
                baseline = max(left['bounds'][h_axis][1], right['bounds'][h_axis][1])
                line_offset = offset + spacing * (index - 1)
                spec = _auto_dim_spec(name,
                                      viewport, mode, baseline, left['center'][axis],
                                      baseline, right['center'][axis], line_offset,
                                      axis, sources, 'contacts')
            emit(spec)

    dimensions.sort(key=lambda row: row['name'])
    derivations.sort(key=lambda row: row['name'])
    payload = {'document': _safe(lambda: vs.GetFPathName()) or '',
               'scope': {'layer': layer, 'name_prefix': prefix},
               'plane': plane, 'viewport': viewport, 'rules': rules,
               'center_axis': center_axis or None,
               'viewport_view': {'projection': projection, 'view_type': view_type},
               'dimensions': dimensions, 'derivations': derivations}
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return {'status': 'ok', 'read_only': True,
            'snapshot_digest': hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20],
            'object_count': len(snapshot), 'dimension_count': len(dimensions),
            'plane': plane, 'package': {'dimensions': dimensions},
            'coordinate_space': ('model coordinates; AddVPAnnotationObject applies the '
                                 'viewport-center transform while preserving model values'),
            'derivations': derivations,
            'limitations': ('axis-aligned named 3D geometry only; contacts require one '
                            'unambiguous shared face; no dimensions are written')}


_SEMANTIC_ALIASES = {
    'lumber': {'wood', 'frame', 'framing', 'stud', 'joist', 'rail', 'plate', 'post'},
    'wood': {'lumber', 'frame', 'framing', 'stud', 'joist', 'rail', 'plate', 'post'},
    'cabinet': {'case', 'carcass', 'shelf', 'side', 'back', 'casework'},
    'cabinetry': {'cabinet', 'case', 'carcass', 'shelf', 'casework'},
    'vertical': {'z', 'post', 'stud'},
    'horizontal': {'x', 'y', 'rail', 'plate', 'joist'},
}


def _semantic_terms(value):
    import re
    return [part for part in re.findall(r'[a-z0-9]+', str(value or '').lower()) if part]


def _semantic_list(value, field):
    if value is None: return [], None
    if isinstance(value, str): value = [value]
    if not isinstance(value, list) or len(value) > 100:
        return None, '%s must be a string/list of at most 100 values' % field
    rows = [str(item).strip().lower() for item in value]
    if any(not item for item in rows): return None, '%s cannot contain empty values' % field
    return rows, None


def _semantic_range(raw, field):
    if raw is None: return {}, None
    if not isinstance(raw, dict) or set(raw) - {'min', 'max'}:
        return None, '%s range must contain only min/max' % field
    out = {}
    for key in ('min', 'max'):
        if key in raw:
            try: out[key] = float(raw[key])
            except Exception: return None, '%s.%s must be numeric' % (field, key)
            if not math.isfinite(out[key]): return None, '%s.%s must be finite' % (field, key)
    if 'min' in out and 'max' in out and out['min'] > out['max']:
        return None, '%s min exceeds max' % field
    return out, None


def _semantic_search_plan(p):
    layer = str(p.get('layer') or '').strip()
    query = str(p.get('query') or '').strip()
    filters = p.get('filters') or {}
    if not layer: return None, 'layer is required safety scope'
    if not isinstance(filters, dict): return None, 'filters must be an object'
    allowed_filters = {'classes', 'roles', 'axes', 'types', 'materials',
                       'name_prefix', 'dimensions', 'center'}
    unknown = sorted(set(filters) - allowed_filters)
    if unknown: return None, 'unsupported semantic filters: %s' % ', '.join(unknown)
    if not query and not filters: return None, 'query or at least one filter is required'
    if len(query) > 200: return None, 'query exceeds 200 characters'
    try:
        limit = int(p.get('limit', 50)); candidate_limit = int(p.get('candidate_limit', 500))
        guard = int(p.get('guard', 60000))
    except Exception: return None, 'limit/candidate_limit/guard must be integers'
    if not 1 <= limit <= 200 or not 1 <= candidate_limit <= 1000 or limit > candidate_limit:
        return None, 'limit must be 1..200 and <= candidate_limit 1..1000'
    if not 1 <= guard <= 200000: return None, 'guard must be 1..200000'
    mode = str(p.get('mode') or 'all').strip().lower()
    if mode not in ('all', 'any'): return None, 'mode must be all or any'

    normalized = {}
    for field in ('classes', 'roles', 'axes', 'types', 'materials'):
        normalized[field], error = _semantic_list(filters.get(field), field)
        if error: return None, error
    normalized['name_prefix'] = str(filters.get('name_prefix') or '').strip()
    normalized['dimensions'] = {}; normalized['center'] = {}
    for group in ('dimensions', 'center'):
        raw_group = filters.get(group) or {}
        if not isinstance(raw_group, dict) or set(raw_group) - {'x', 'y', 'z'}:
            return None, '%s must contain only x/y/z ranges' % group
        for axis, raw_range in raw_group.items():
            normalized[group][axis], error = _semantic_range(raw_range, '%s.%s' % (group, axis))
            if error: return None, error

    manifest_params = {'layer': layer, 'limit': candidate_limit + 1,
                       'guard': guard}
    if normalized['name_prefix']: manifest_params['name_prefix'] = normalized['name_prefix']
    if normalized['roles']: manifest_params['roles'] = normalized['roles']
    manifest = get_assembly_manifest(manifest_params)
    if manifest.get('error'): return None, manifest['error']
    if manifest.get('truncated'):
        return None, 'semantic candidate scope exceeds candidate_limit; add a filter'
    query_terms = _semantic_terms(query)
    matches = []
    weights = {'name': 6, 'class': 5, 'role': 5, 'profile': 4,
               'material': 4, 'texture': 3, 'type_name': 2, 'layer': 1, 'axis': 2}

    def range_clean(value, wanted):
        if value is None: return False
        return (('min' not in wanted or float(value) >= wanted['min']) and
                ('max' not in wanted or float(value) <= wanted['max']))

    for row in manifest.get('objects') or []:
        if normalized['classes'] and str(row.get('class') or '').lower() not in normalized['classes']:
            continue
        if normalized['roles'] and str(row.get('role') or '').lower() not in normalized['roles']:
            continue
        if normalized['axes'] and str(row.get('axis') or '').lower() not in normalized['axes']:
            continue
        if normalized['types'] and (str(row.get('type_name') or '').lower() not in normalized['types'] and
                                    str(row.get('type') or '').lower() not in normalized['types']):
            continue
        pio = row.get('pio') or {}
        material = str(row.get('material') or pio.get('MemberMaterial') or '').lower()
        if normalized['materials'] and material not in normalized['materials']:
            continue
        failed = False
        for group in ('dimensions', 'center'):
            values = row.get(group) or {}
            if any(not range_clean(values.get(axis), wanted)
                   for axis, wanted in normalized[group].items()):
                failed = True; break
        if failed: continue

        field_values = {
            'name': row.get('name'), 'class': row.get('class'), 'role': row.get('role'),
            'profile': row.get('profile') or row.get('symbol_name'),
            'material': material, 'texture': row.get('texture'),
            'type_name': row.get('type_name'), 'layer': row.get('layer'),
            'axis': row.get('axis'),
        }
        field_tokens = {field: set(_semantic_terms(value)) for field, value in field_values.items()}
        term_hits, reasons, score = [], [], 0
        for term in query_terms:
            alternatives = [term] + sorted(_SEMANTIC_ALIASES.get(term, set()) - {term})
            best = None
            for field, tokens in field_tokens.items():
                hit = next((candidate for candidate in alternatives
                            if any(candidate == token or
                                   (len(candidate) >= 3 and
                                    (candidate in token or token in candidate))
                                   for token in tokens)), None)
                quality = weights[field] + (4 if hit == term else 0)
                if hit and (best is None or quality > best[0]):
                    best = (quality, field, hit)
            term_hits.append(bool(best))
            if best:
                score += best[0]; reasons.append('%s→%s:%s' % (term, best[1], best[2]))
        if query_terms and ((mode == 'all' and not all(term_hits)) or
                            (mode == 'any' and not any(term_hits))):
            continue
        if not query_terms: reasons.append('structured filters')
        matches.append({
            'object_id': row.get('object_id'), 'name': row.get('name') or '',
            'parent_id': row.get('parent_id'),
            'selection_id': row.get('parent_id') or row.get('object_id'),
            'selection_level': 'container' if row.get('parent_id') else 'object',
            'type': row.get('type'), 'type_name': row.get('type_name'),
            'class': row.get('class'), 'layer': row.get('layer'),
            'role': row.get('role'), 'axis': row.get('axis'),
            'center': row.get('center'), 'dimensions': row.get('dimensions'),
            'material': material or None, 'score': score, 'reasons': reasons,
        })
    matches.sort(key=lambda row: (-row['score'], row['name'], str(row['object_id'] or '')))
    total_match_count = len(matches)
    truncated = total_match_count > limit
    matches = matches[:limit]
    payload = {'document': _safe(lambda: vs.GetFPathName()) or '', 'layer': layer,
               'query': query, 'filters': normalized, 'mode': mode,
               'total_match_count': total_match_count, 'truncated': truncated,
               'matches': matches}
    return {'status': 'ok', 'read_only': True, 'layer': layer, 'query': query,
            'filters': normalized, 'mode': mode,
            'candidate_count': len(manifest.get('objects') or []),
            'match_count': len(matches), 'total_match_count': total_match_count,
            'truncated': truncated, 'matches': matches,
            'search_token': _revision_digest(payload)}, None


def semantic_object_search(p):
    result, error = _semantic_search_plan(p)
    return {'error': error} if error else result


def select_semantic_objects(p):
    search, error = _semantic_search_plan(p)
    if error: return {'error': error}
    replace = bool(p.get('replace', True))
    def canonical_selected_ids(handles):
        rows = list(handles); result = []
        for handle in rows:
            parent = _safe(lambda handle=handle: vs.GetParent(handle))
            nested = False; guard = 0
            while parent and guard < 100:
                if parent in rows: nested = True; break
                parent = _safe(lambda parent=parent: vs.GetParent(parent))
                guard += 1
            object_id = _oid(handle)
            if object_id and not nested: result.append(object_id)
        return sorted(set(result))

    selected_before = canonical_selected_ids(_collect('SEL=TRUE', 1001))
    if len(selected_before) > 1000:
        return {'error': 'current selection exceeds safety limit'}
    payload = {'document': _safe(lambda: vs.GetFPathName()) or '',
               'search_token': search['search_token'], 'replace': replace,
               'selected_before': selected_before}
    token = _revision_digest(payload)
    target_ids = []
    for row in search['matches']:
        selection_id = row.get('selection_id') or row.get('object_id')
        if selection_id and selection_id not in target_ids: target_ids.append(selection_id)
    base = {'status': 'dry_run', 'plan_token': token, 'replace': replace,
            'selected_before': selected_before, 'target_ids': target_ids,
            'match_count': len(target_ids), 'search': search}
    if not bool(p.get('confirm', False)): return base
    if str(p.get('plan_token') or '') != token:
        return {**base, 'status': 'blocked',
                'error': 'plan_token missing or stale; run a fresh dry-run'}
    if search.get('truncated'):
        return {**base, 'status': 'blocked',
                'error': 'semantic matches are truncated; raise limit or refine query'}
    def apply_selection(object_ids, clear):
        if clear:
            for selected in _collect('SEL=TRUE', 1001):
                vs.SetDSelect(selected)
        for object_id in object_ids:
            handle = _h(object_id)
            if not handle: return False
            vs.SetSelect(handle)
        return True

    if replace and not apply_selection([], True):
        return {'status': 'error', 'error': 'could not clear existing selection'}
    for object_id in target_ids:
        h = _h(object_id)
        if not h:
            apply_selection(selected_before, True)
            restored = canonical_selected_ids(_collect('SEL=TRUE', 1001))
            return {'status': 'error', 'error': 'matched object disappeared',
                    'rollback_clean': restored == selected_before}
        vs.SetSelect(h)
    selected_after = canonical_selected_ids(_collect('SEL=TRUE', 1001))
    expected = sorted(set(target_ids) if replace else set(selected_before) | set(target_ids))
    if selected_after != expected:
        apply_selection(selected_before, True)
        restored = canonical_selected_ids(_collect('SEL=TRUE', 1001))
        return {'status': 'error', 'error': 'selection readback mismatch',
                'expected': expected, 'actual': selected_after,
                'rollback_clean': restored == selected_before}
    return {'status': 'ok', 'changed': selected_after != selected_before,
            'plan_token': token, 'selected_count': len(selected_after),
            'selected_ids': selected_after, 'search_token': search['search_token'],
            'selection_scope': ('replace=true clears the document-wide selection; '
                                'matched targets remain layer-scoped')}


def _document_record_summary(h, field_limit):
    rows, used = [], 0
    count = int(_safe(lambda: vs.NumRecords(h), 0) or 0)
    for index in range(1, count + 1):
        record = _safe(lambda index=index: vs.GetRecord(h, index))
        name = str(_safe(lambda record=record: vs.GetName(record), '') or '')
        if not record or not name: continue
        fields = {}
        field_count = int(_safe(lambda record=record: vs.NumFields(record), 0) or 0)
        for field_index in range(1, field_count + 1):
            if used >= field_limit: break
            field = str(_safe(lambda record=record, field_index=field_index:
                              vs.GetFldName(record, field_index), '') or '')
            if not field: continue
            fields[field] = str(_safe(lambda name=name, field=field:
                                      vs.GetRField(h, name, field), '') or '')
            used += 1
        rows.append({'name': name, 'fields': fields,
                     'field_count': field_count,
                     'fields_truncated': len(fields) < field_count})
        if used >= field_limit: break
    return rows, used >= field_limit


def index_document(p):
    """Build a bounded semantic index across layers, containers, annotations, and records."""
    requested = p.get('layers')
    if isinstance(requested, str): requested = [requested]
    if requested is not None and not isinstance(requested, list):
        return {'error': 'layers must be a string/list'}
    try:
        limit = int(p.get('limit', 2000)); guard = int(p.get('guard', 100000))
        max_depth = int(p.get('max_depth', 25)); field_limit = int(p.get('record_field_limit', 1000))
    except Exception:
        return {'error': 'limit/guard/max_depth/record_field_limit must be integers'}
    if not 1 <= limit <= 5000 or not 1 <= guard <= 200000:
        return {'error': 'limit must be 1..5000 and guard 1..200000'}
    if not 0 <= max_depth <= 100 or not 0 <= field_limit <= 10000:
        return {'error': 'max_depth must be 0..100 and record_field_limit 0..10000'}
    include_annotations = bool(p.get('include_annotations', True))
    include_records = bool(p.get('include_records', True))
    include_symbols = bool(p.get('include_symbols', True))
    available = [row for row in get_layers({}).get('layers') or []
                 if row.get('type') in ('design', 'sheet')]
    available_map = {row['name']: row for row in available}
    layer_names = [str(value).strip() for value in requested] if requested is not None else sorted(available_map)
    if any(not name for name in layer_names): return {'error': 'layers cannot contain empty names'}
    missing = sorted(set(layer_names) - set(available_map))
    if missing: return {'error': 'layers not found: %s' % ', '.join(missing)}
    if len(set(layer_names)) != len(layer_names): return {'error': 'layers must be unique'}
    rows, errors, visited = [], [], [0]
    record_fields_used = [0]
    truncated_records = [False]
    truncated_limit, truncated_guard, truncated_depth = [False], [False], [False]
    container_types = {11}
    safe_center_types = {15, 24, 25, 26, 28, 34, 68, 84, 86, 89, 91, 122}
    safe_dimension_types = safe_center_types | {40}

    def append_row(h, layer_name, depth, parent_id, container_kind=None):
        if len(rows) >= limit:
            truncated_limit[0] = True; return False
        object_type = int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0)
        dims, _raw = _dims3(h) if object_type in safe_dimension_types else (None, None)
        center = _center3(h) if object_type in safe_center_types else None
        records, records_truncated = ([], False)
        record_count = int(_safe(lambda h=h: vs.NumRecords(h), 0) or 0) if include_records else 0
        if include_records and record_fields_used[0] < field_limit:
            records, records_truncated = _document_record_summary(
                h, max(0, field_limit - record_fields_used[0]))
            record_fields_used[0] += sum(len(row['fields']) for row in records)
            if records_truncated: truncated_records[0] = True
        elif record_count:
            records_truncated = True
            truncated_records[0] = True
        row = {
            'object_id': _oid(h), 'name': str(_safe(lambda h=h: vs.GetName(h), '') or ''),
            'type': object_type, 'type_name': OBJ_TYPES.get(object_type, 'type_%s' % object_type),
            'class': str(_safe(lambda h=h: vs.GetClass(h), '') or ''),
            'layer': layer_name, 'depth': depth, 'parent_id': parent_id,
            'container_kind': container_kind,
            'is_container': object_type in container_types or object_type in (12,),
            'selected': bool(_safe(lambda h=h: vs.Selected(h), False)),
            'bounds': _bbox(h), 'center': center, 'dimensions': dims,
            'plugin_name': str(_pio_record(h) or ''),
            'symbol_name': str(_safe(lambda h=h: vs.GetSymName(h), '') or '') if object_type == 15 else '',
            'records': records, 'records_truncated': records_truncated,
        }
        rows.append(row)
        return True

    def walk(h, layer_name, depth=0, parent_id=None, container_kind=None):
        while h:
            if visited[0] >= guard:
                truncated_guard[0] = True; return
            if len(rows) >= limit:
                truncated_limit[0] = True; return
            visited[0] += 1
            object_type = int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0)
            object_id = _oid(h)
            try:
                if not append_row(h, layer_name, depth, parent_id, container_kind): return
                if object_type in container_types:
                    if depth >= max_depth:
                        truncated_depth[0] = True
                    else:
                        child = _safe(lambda h=h: vs.FInGroup(h))
                        if child: walk(child, layer_name, depth + 1, object_id, 'group')
                if include_annotations and object_type in (89, 122):
                    annotation_group = _safe(lambda h=h: vs.GetVPGroup(h, 2))
                    child = _safe(lambda annotation_group=annotation_group:
                                  vs.FInGroup(annotation_group)) if annotation_group else None
                    if child and depth < max_depth:
                        walk(child, layer_name, depth + 1, object_id, 'viewport_annotations')
            except Exception as exc:
                errors.append({'object_id': object_id, 'error': str(exc)})
            h = _safe(lambda h=h: vs.NextObj(h))

    for layer_name in layer_names:
        layer = _safe(lambda layer_name=layer_name: vs.GetLayerByName(layer_name))
        if layer: walk(_safe(lambda layer=layer: vs.FInLayer(layer)), layer_name)
        if truncated_limit[0] or truncated_guard[0]: break
    symbol_definitions = []
    if include_symbols and not truncated_limit[0] and not truncated_guard[0]:
        symbol = _safe(lambda: vs.FSymDef())
        while symbol:
            if visited[0] >= guard:
                truncated_guard[0] = True; break
            if len(rows) >= limit:
                truncated_limit[0] = True; break
            name = str(_safe(lambda symbol=symbol: vs.GetName(symbol), '') or '')
            object_id = _oid(symbol)
            symbol_definitions.append({'entity_kind': 'symbol_definition',
                                       'object_id': object_id, 'name': name})
            child = _safe(lambda symbol=symbol: vs.FInSymDef(symbol))
            if child and max_depth > 0:
                walk(child, '', 1, object_id, 'symbol_definition')
            elif child:
                truncated_depth[0] = True
            symbol = _safe(lambda symbol=symbol: vs.NextSymDef(symbol))
    catalog = []
    for layer_name in layer_names:
        info = available_map[layer_name]
        catalog.append({'entity_kind': 'layer',
                        'entity_id': 'layer:' + hashlib.sha256(layer_name.encode('utf-8')).hexdigest()[:20],
                        'name': layer_name, 'layer': layer_name,
                        'type_name': str(info.get('type') or 'layer'),
                        'visibility': info.get('visible'), 'scale': info.get('scale')})
    for class_name, info in sorted(_class_state_map().items()):
        catalog.append({'entity_kind': 'class',
                        'entity_id': 'class:' + hashlib.sha256(class_name.encode('utf-8')).hexdigest()[:20],
                        'name': class_name, 'class': class_name, 'type_name': 'class',
                        'visibility': info.get('visibility')})
    catalog.extend(symbol_definitions)
    rows.sort(key=lambda row: (row['layer'], row['depth'], row['name'], row['type'],
                               row['object_id'] or ''))
    payload = {
        'schema': 1, 'document': _safe(lambda: vs.GetFPathName()) or '',
        'layers': layer_names, 'include_annotations': include_annotations,
        'include_records': include_records, 'include_symbols': include_symbols,
        'rows': rows, 'catalog': catalog,
        'coverage': {'visited': visited[0], 'returned': len(rows),
                     'max_depth': max_depth, 'record_fields': record_fields_used[0]},
        'truncated': bool(truncated_limit[0] or truncated_guard[0] or
                          truncated_depth[0] or truncated_records[0]),
        'truncation': {'limit': truncated_limit[0], 'guard': truncated_guard[0],
                       'depth': truncated_depth[0], 'records': truncated_records[0]},
        'errors': errors,
    }
    return {**payload, 'status': 'ok', 'read_only': True,
            'index_token': _revision_digest(payload), 'count': len(rows)}


def search_document(p):
    """Search a fresh universal document index with semantic terms and safe filters."""
    query = str(p.get('query') or '').strip()
    filters = p.get('filters') or {}
    if not query and not filters: return {'error': 'query or filters required'}
    if len(query) > 200 or not isinstance(filters, dict):
        return {'error': 'query exceeds 200 characters or filters is not an object'}
    allowed = {'layers', 'classes', 'types', 'names', 'record_names', 'selected'}
    unknown = sorted(set(filters) - allowed)
    if unknown: return {'error': 'unsupported filters: %s' % ', '.join(unknown)}
    try: limit = int(p.get('limit', 100))
    except Exception: return {'error': 'limit must be an integer'}
    if not 1 <= limit <= 500: return {'error': 'limit must be 1..500'}
    indexed = index_document({
        'layers': p.get('layers'), 'limit': p.get('index_limit', 2000),
        'guard': p.get('guard', 100000), 'max_depth': p.get('max_depth', 25),
        'record_field_limit': p.get('record_field_limit', 1000),
        'include_annotations': p.get('include_annotations', True),
        'include_records': p.get('include_records', True),
        'include_symbols': p.get('include_symbols', True),
    })
    if indexed.get('error'): return {'error': indexed['error']}
    if indexed.get('truncated'): return {'error': 'document index truncated; narrow layers or raise bounded limit'}
    if indexed.get('errors'): return {'error': 'document index contains read errors'}
    normalized = {}
    for key in ('layers', 'classes', 'types', 'names', 'record_names'):
        value = filters.get(key)
        if value is None: normalized[key] = []
        else:
            if isinstance(value, str): value = [value]
            if not isinstance(value, list) or len(value) > 200:
                return {'error': '%s must be a string/list of at most 200 values' % key}
            normalized[key] = [str(item).strip().lower() for item in value]
    if 'selected' in filters and not isinstance(filters['selected'], bool):
        return {'error': 'selected filter must be boolean'}
    terms = _semantic_terms(query); matches = []
    weights = {'name': 8, 'record': 6, 'class': 5, 'symbol': 5,
               'plugin': 4, 'type': 3, 'layer': 2}
    search_rows = list(indexed['rows'])
    for item in indexed.get('catalog') or []:
        search_rows.append({
            'object_id': item.get('object_id'), 'entity_id': item.get('entity_id'),
            'entity_kind': item.get('entity_kind'), 'name': item.get('name') or '',
            'type': None, 'type_name': item.get('type_name') or item.get('entity_kind') or '',
            'class': item.get('class') or '', 'layer': item.get('layer') or '',
            'selected': False, 'records': [], 'symbol_name': '', 'plugin_name': '',
        })
    for row in search_rows:
        if normalized['layers'] and row['layer'].lower() not in normalized['layers']: continue
        if normalized['classes'] and row['class'].lower() not in normalized['classes']: continue
        if normalized['types'] and (str(row['type']).lower() not in normalized['types'] and
                                    row['type_name'].lower() not in normalized['types']): continue
        if normalized['names'] and row['name'].lower() not in normalized['names']: continue
        record_names = [str(record.get('name') or '').lower() for record in row.get('records') or []]
        if normalized['record_names'] and not set(normalized['record_names']).intersection(record_names): continue
        if 'selected' in filters and row['selected'] != filters['selected']: continue
        record_text = ' '.join(record_names + [str(value) for record in row.get('records') or []
                                               for value in (record.get('fields') or {}).values()])
        fields = {'name': row['name'], 'record': record_text, 'class': row['class'],
                  'symbol': row['symbol_name'], 'plugin': row['plugin_name'],
                  'type': row['type_name'], 'layer': row['layer']}
        tokens = {key: set(_semantic_terms(value)) for key, value in fields.items()}
        score, reasons, hit_all = 0, [], True
        for term in terms:
            alternatives = [term] + sorted(_SEMANTIC_ALIASES.get(term, set()) - {term})
            best = None
            for field, values in tokens.items():
                hit = next((candidate for candidate in alternatives
                            if any(candidate == value or (len(candidate) >= 3 and len(value) >= 3 and
                                   (candidate in value or value in candidate)) for value in values)), None)
                quality = weights[field] + (4 if hit == term else 0)
                if hit and (best is None or quality > best[0]): best = (quality, field, hit)
            if not best: hit_all = False; break
            score += best[0]; reasons.append('%s→%s:%s' % (term, best[1], best[2]))
        if terms and not hit_all: continue
        matches.append({**row, 'score': score, 'reasons': reasons or ['structured filters']})
    matches.sort(key=lambda row: (-row['score'], row['layer'], row['name'], row['object_id'] or ''))
    total = len(matches); truncated = total > limit
    return {'status': 'ok', 'read_only': True, 'query': query, 'filters': normalized,
            'index_token': indexed['index_token'], 'candidate_count': indexed['count'],
            'total_match_count': total, 'match_count': min(total, limit),
            'truncated': truncated, 'matches': matches[:limit],
            'coverage': indexed['coverage']}


_REVISION_BASELINES = globals().get('_REVISION_BASELINES', {})
_REVISION_BASELINE_ROOT_OVERRIDE = globals().get('_REVISION_BASELINE_ROOT_OVERRIDE')


def _revision_quantize(value, quantum):
    if isinstance(value, float):
        result = round(value / quantum) * quantum
        return 0.0 if result == 0 else result
    if isinstance(value, dict):
        return {key: _revision_quantize(value[key], quantum) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_revision_quantize(item, quantum) for item in value]
    return value


def _revision_digest(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]


def _revision_snapshot_payload(snapshot):
    return {key: snapshot[key] for key in (
        'schema', 'document', 'units_per_inch', 'quantum', 'domains',
        'scope', 'rows', 'partial', 'limitations') if key in snapshot}


def _valid_revision_snapshot(snapshot):
    return (isinstance(snapshot, dict) and bool(snapshot.get('digest')) and
            _revision_digest(_revision_snapshot_payload(snapshot)) == snapshot['digest'])


def _revision_baseline_root():
    override = globals().get('_REVISION_BASELINE_ROOT_OVERRIDE')
    return (str(override) if override else os.path.join(
        os.path.expanduser('~'), 'Library', 'Application Support',
        'Vectorworks', '2026', 'VWX-MCP-Baselines'))


def _revision_baseline_document_dir():
    document = str(_safe(lambda: vs.GetFPathName(), '') or '')
    if not document: return None
    identity = hashlib.sha256(document.encode('utf-8')).hexdigest()[:24]
    return os.path.join(_revision_baseline_root(), identity)


def _revision_baseline_path(name):
    directory = _revision_baseline_document_dir()
    if not directory: return None
    filename = hashlib.sha256(name.encode('utf-8')).hexdigest() + '.json'
    return os.path.join(directory, filename)


def _revision_cache_key(name):
    document = str(_safe(lambda: vs.GetFPathName(), '') or '')
    return '%s|%s' % (hashlib.sha256(document.encode('utf-8')).hexdigest()[:24],
                      hashlib.sha256(name.encode('utf-8')).hexdigest())


def _revision_cache_get(name):
    snapshot = _REVISION_BASELINES.get(_revision_cache_key(name))
    document = str(_safe(lambda: vs.GetFPathName(), '') or '')
    return snapshot if isinstance(snapshot, dict) and snapshot.get('document') == document else None


def _load_persistent_revision_baseline(name):
    path = _revision_baseline_path(name)
    if not path or not os.path.isfile(path): return None, None
    try:
        with open(path, 'r', encoding='utf-8') as handle: payload = json.load(handle)
    except Exception as exc:
        return None, 'baseline file unreadable: %s' % exc
    if not isinstance(payload, dict) or payload.get('name') != name:
        return None, 'baseline file name mismatch'
    snapshot = payload.get('snapshot')
    if not _valid_revision_snapshot(snapshot): return None, 'baseline file digest is invalid'
    if snapshot.get('document') != (_safe(lambda: vs.GetFPathName()) or ''):
        return None, 'baseline belongs to a different document'
    return snapshot, None


def _persistent_revision_baseline_entries():
    directory = _revision_baseline_document_dir()
    if not directory or not os.path.isdir(directory): return [], []
    rows, errors = [], []
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith('.json'): continue
        path = os.path.join(directory, filename)
        try:
            with open(path, 'r', encoding='utf-8') as handle: payload = json.load(handle)
            name = str(payload.get('name') or '')
            snapshot = payload.get('snapshot')
            if not name or not _valid_revision_snapshot(snapshot): raise ValueError('invalid payload')
            rows.append({'name': name, 'digest': snapshot.get('digest'),
                         'stored_at': payload.get('stored_at')})
        except Exception as exc:
            errors.append({'file': filename, 'error': str(exc)})
    rows.sort(key=lambda row: row['name'])
    return rows, errors


def get_revision_snapshot(p):
    """Return deterministic JSON state across geometry/structure/drawing/reports."""
    domains = p.get('domains') or ['structure', 'drawing']
    if not isinstance(domains, list) or not domains:
        return {'error': 'domains must be a non-empty list'}
    domains = [str(value).strip().lower() for value in domains]
    unknown = sorted(set(domains) - {'geometry', 'structure', 'drawing', 'reports'})
    if unknown: return {'error': 'unsupported revision domains: %s' % ', '.join(unknown)}
    if len(set(domains)) != len(domains): return {'error': 'domains must be unique'}
    try:
        quantum = abs(float(p.get('quantum', 0.001)))
        max_cells = int(p.get('max_cells', 1000))
        drawing_limit = int(p.get('drawing_limit', 100))
        visibility_limit = int(p.get('visibility_limit', 20000))
        geometry_limit = int(p.get('geometry_limit', 500))
        guard = int(p.get('guard', 60000))
        structure_limit = int(p.get('structure_limit', 1000))
    except Exception: return {'error': 'quantum/max_cells values are invalid'}
    if (quantum <= 0 or not 1 <= max_cells <= 5000 or
            not 1 <= drawing_limit <= 500 or not 1 <= visibility_limit <= 50000):
        return {'error': ('quantum must be positive; max_cells 1..5000; '
                          'drawing_limit 1..500; visibility_limit 1..50000')}
    if not 1 <= geometry_limit <= 2000 or not 1 <= guard <= 200000:
        return {'error': 'geometry_limit must be 1..2000 and guard 1..200000'}
    if not 1 <= structure_limit <= 5000:
        return {'error': 'structure_limit must be 1..5000'}
    resource_targets = p.get('resource_targets') or []
    report_names = p.get('report_names') or []
    geometry_scopes = p.get('geometry_scopes')
    if not isinstance(resource_targets, list) or len(resource_targets) > 200:
        return {'error': 'resource_targets must be a list of at most 200 items'}
    if any(not isinstance(item, dict) for item in resource_targets):
        return {'error': 'resource_targets must contain only objects'}
    if not isinstance(report_names, list) or len(report_names) > 100:
        return {'error': 'report_names must be a list of at most 100 items'}
    rows = {domain: [] for domain in domains}
    partial, limitations = False, []

    normalized_geometry_scopes = []
    if 'geometry' in rows:
        if geometry_scopes is None:
            geometry_scopes = [{'layer': p.get('layer'), 'name_prefix': p.get('name_prefix')}]
        if not isinstance(geometry_scopes, list) or not geometry_scopes or len(geometry_scopes) > 50:
            return {'error': 'geometry_scopes must be a non-empty list of at most 50 scopes'}
        seen_scopes = set()
        remaining_limit = geometry_limit
        for index, raw_scope in enumerate(geometry_scopes):
            if not isinstance(raw_scope, dict) or set(raw_scope) - {'layer', 'name_prefix'}:
                return {'error': 'geometry scope %d supports only layer/name_prefix' % index}
            layer = str(raw_scope.get('layer') or '').strip()
            prefix = str(raw_scope.get('name_prefix') or '').strip()
            if not layer or not prefix: return {'error': 'each geometry scope requires layer and name_prefix'}
            key = (layer, prefix)
            if key in seen_scopes: return {'error': 'geometry scopes must be unique'}
            seen_scopes.add(key); normalized_geometry_scopes.append({'layer': layer, 'name_prefix': prefix})
            manifest = get_assembly_manifest({
                'layer': layer, 'name_prefix': prefix,
                'limit': max(1, remaining_limit + 1), 'guard': guard,
            })
            if manifest.get('error'): return {'error': manifest['error']}
            if len(manifest.get('objects') or []) > remaining_limit:
                return {'error': 'geometry snapshot exceeds geometry_limit'}
            if manifest.get('truncated_by_guard'):
                return {'error': 'geometry snapshot truncated; narrow scope'}
            for item in manifest.get('objects') or []:
                name = str(item.get('name') or '')
                if not name: return {'error': 'geometry snapshot contains unnamed object'}
                row = {field: item.get(field) for field in
                       ('name', 'type', 'type_name', 'class', 'layer', 'role', 'axis',
                        'center', 'dimensions', 'length', 'material', 'texture')}
                row['key'] = '%s|%s|%s' % (layer, prefix, name)
                rows['geometry'].append(_revision_quantize(row, quantum))
            remaining_limit -= len(manifest.get('objects') or [])
            if remaining_limit < 0: return {'error': 'geometry snapshot exceeds geometry_limit'}

    if 'structure' in rows:
        layer_states = _layer_state_map(); class_states = _class_state_map()
        if len(layer_states) + len(class_states) + len(resource_targets) > structure_limit:
            return {'error': 'structure snapshot exceeds structure_limit'}
        for name, item in layer_states.items():
            row = {key: item.get(key) for key in
                   ('name', 'type', 'scale', 'visible', 'elevation', 'thickness')}
            row['key'] = 'layer|%s' % name
            rows['structure'].append(_revision_quantize(row, quantum))
        for name, item in class_states.items():
            row = {'key': 'class|%s' % name, 'name': name, 'kind': 'class',
                   'fill': item.get('fill'), 'pen': item.get('pen'),
                   'lineweight': item.get('lineweight'), 'visible': item.get('visible')}
            rows['structure'].append(_revision_quantize(row, quantum))
        for item in resource_targets:
            if not isinstance(item, dict): return {'error': 'resource_targets must be objects'}
            kind = str(item.get('kind') or '').strip().lower()
            name = str(item.get('name') or '').strip()
            if kind not in _RESOURCE_KINDS or not name:
                return {'error': 'resource target requires supported kind and name'}
            state = _resource_document_state(kind, name)
            rows['structure'].append({'key': 'resource|%s|%s' % (kind, name),
                                      'kind': kind, 'name': name,
                                      'state': state.get('state'),
                                      'actual_type': state.get('actual_type')})

    if 'drawing' in rows:
        for name, item in _drawing_sheet_map().items():
            rows['drawing'].append(_revision_quantize({
                'key': 'sheet|%s' % name, 'kind': 'sheet', 'name': name,
                'title': item.get('title'), 'dpi': item.get('dpi')}, quantum))
        all_layers = [row['name'] for row in get_layers({}).get('layers') or []
                      if row.get('type') == 'design']
        all_classes = _class_names_all()
        viewport_count = int(_safe(lambda: vs.Count('T=VIEWPORT'), 0) or 0)
        if viewport_count > drawing_limit:
            return {'error': 'viewport count exceeds drawing_limit'}
        if viewport_count * (len(all_layers) + len(all_classes)) > visibility_limit:
            return {'error': 'viewport visibility snapshot exceeds visibility_limit'}
        for h in _collect('T=VIEWPORT'):
            name = str(_safe(lambda h=h: vs.GetName(h), '') or '')
            if not name: continue
            layer_visibility = {layer: _vp_visibility_value(_safe(
                lambda h=h, layer=layer: vs.GetVPLayerVisibility(
                    h, vs.GetLayerByName(layer)))) for layer in all_layers}
            class_visibility = {cls: _vp_visibility_value(_safe(
                lambda h=h, cls=cls: vs.GetVPClassVisibility(h, cls)))
                                for cls in all_classes}
            rows['drawing'].append(_revision_quantize({
                'key': 'viewport|%s' % name, 'kind': 'viewport', 'name': name,
                'sheet': str(_safe(lambda h=h: vs.GetLName(vs.GetLayer(h)), '') or ''),
                'scale': _safe(lambda h=h: vs.GetObjectVariableReal(h, 1003)),
                'projection': _safe(lambda h=h: vs.GetObjectVariableInt(h, 1000)),
                'view_type': _safe(lambda h=h: vs.GetObjectVariableInt(h, 1007)),
                'center': _viewport_center(h), 'layers': layer_visibility,
                'classes': class_visibility,
            }, quantum))
        saved = _saved_view_names_strict()
        if saved is None:
            partial = True; limitations.append('Saved View list unreadable')
        else:
            for name in saved:
                rows['drawing'].append({'key': 'saved_view|%s' % name,
                                        'kind': 'saved_view', 'name': name,
                                        'verification_level': 'name_only_api_limit'})

    if 'reports' in rows:
        requested = report_names
        if not isinstance(requested, list) or not requested:
            return {'error': 'reports snapshot requires a non-empty report_names list'}
        names = [str(value).strip() for value in requested]
        if any(not name for name in names):
            return {'error': 'report_names cannot contain empty values'}
        total_cells = 0
        for name in sorted(set(names)):
            ws = _safe(lambda name=name: vs.GetObject(name))
            if not ws or int(_safe(lambda ws=ws: vs.GetTypeN(ws), 0) or 0) != 18:
                rows['reports'].append({'key': 'worksheet|%s' % name,
                                        'kind': 'worksheet', 'name': name,
                                        'state': 'missing'})
                continue
            size = _safe(lambda ws=ws: vs.GetWSRowColumnCount(ws))
            if not isinstance(size, (list, tuple)) or len(size) < 2:
                return {'error': 'worksheet size unreadable: %s' % name}
            row_count, col_count = int(size[0]), int(size[1])
            total_cells += row_count * col_count
            if total_cells > max_cells:
                return {'error': 'worksheet snapshot exceeds max_cells'}
            rows['reports'].append({'key': 'worksheet|%s' % name,
                                    'kind': 'worksheet', 'name': name,
                                    'rows': row_count, 'columns': col_count})
            for row_num in range(1, row_count + 1):
                subrows = _safe(lambda ws=ws, row_num=row_num:
                                vs.GetWSSubrowCount(ws, row_num))
                if isinstance(subrows, (list, tuple)): subrows = subrows[0] if subrows else 0
                for col_num in range(1, col_count + 1):
                    formula = str(_safe(lambda ws=ws, row_num=row_num, col_num=col_num:
                                        vs.GetWSCellFormula(ws, row_num, col_num), '') or '')
                    text = str(_safe(lambda ws=ws, row_num=row_num, col_num=col_num:
                                     vs.GetWSCellString(ws, row_num, col_num), '') or '')
                    if formula or text or subrows:
                        rows['reports'].append({
                            'key': 'worksheet|%s|r%d|c%d' % (name, row_num, col_num),
                            'kind': 'cell', 'worksheet': name,
                            'row': row_num, 'column': col_num,
                            'formula': formula, 'text': text,
                            'database_subrows': int(subrows or 0),
                        })

    for domain in rows:
        rows[domain].sort(key=lambda row: row['key'])
    units = _safe(lambda: vs.GetUnits())
    units_per_inch = float(units[3]) if isinstance(units, (list, tuple)) and len(units) > 3 else None
    scope = {'layer': str(p.get('layer') or '').strip() or None,
             'name_prefix': str(p.get('name_prefix') or '').strip() or None,
             'geometry_scopes': normalized_geometry_scopes,
             'resource_targets': sorted(resource_targets,
                                        key=lambda row: (str(row.get('kind', '')),
                                                         str(row.get('name', '')))),
             'report_names': sorted(set(str(value).strip() for value in report_names))}
    digest_payload = {'schema': 1, 'document': _safe(lambda: vs.GetFPathName()) or '',
                      'units_per_inch': units_per_inch, 'quantum': quantum,
                      'domains': domains, 'scope': scope, 'rows': rows,
                      'partial': partial, 'limitations': limitations}
    return {**digest_payload, 'status': 'ok', 'read_only': True,
            'taken_at': int(time.time()), 'digest': _revision_digest(digest_payload)}


def manage_revision_baseline(p):
    """Store/list/get/forget create-only persistent revision snapshots."""
    action = str(p.get('action') or 'list').strip().lower()
    name = str(p.get('name') or '').strip()
    if action == 'list':
        persistent, errors = _persistent_revision_baseline_entries()
        return {'status': 'ok', 'storage': 'persistent_file_plus_memory_cache',
                'directory': _revision_baseline_document_dir(),
                'baselines': persistent,
                'errors': errors, 'partial': bool(errors)}
    if action == 'store':
        snapshot = p.get('snapshot')
        if not name or not isinstance(snapshot, dict) or not snapshot.get('digest'):
            return {'error': 'store requires name and revision snapshot'}
        existing, load_error = _load_persistent_revision_baseline(name)
        if load_error: return {'error': load_error}
        if _revision_cache_get(name) is not None or existing is not None:
            return {'error': 'baseline already exists; never overwritten'}
        if len(name) > 100: return {'error': 'baseline name exceeds 100 characters'}
        persistent, persistent_errors = _persistent_revision_baseline_entries()
        if persistent_errors:
            return {'error': 'cannot verify persistent baseline inventory'}
        if len(persistent) >= 20:
            return {'error': 'baseline store is full (20); forget an old baseline'}
        if snapshot.get('partial'):
            return {'error': 'partial snapshots cannot become baselines'}
        if not _valid_revision_snapshot(snapshot):
            return {'error': 'snapshot digest is invalid'}
        if len(json.dumps(snapshot, separators=(',', ':'), default=str)) > 5000000:
            return {'error': 'snapshot exceeds 5 MB baseline limit'}
        path = _revision_baseline_path(name)
        if not path: return {'error': 'document must be saved before storing a baseline'}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {'schema': 1, 'name': name, 'stored_at': int(time.time()),
                   'snapshot': json.loads(json.dumps(snapshot))}
        temp = '%s.tmp.%s' % (path, uuid.uuid4().hex)
        try:
            with open(temp, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, sort_keys=True, separators=(',', ':'))
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, path)
        except Exception as exc:
            _safe(lambda: os.remove(temp))
            return {'error': 'could not persist baseline: %s' % exc}
        verified, verify_error = _load_persistent_revision_baseline(name)
        if verify_error or not verified:
            return {'error': verify_error or 'persistent baseline readback failed'}
        _REVISION_BASELINES[_revision_cache_key(name)] = verified
        return {'status': 'ok', 'stored': name, 'digest': snapshot['digest'],
                'storage': 'persistent_file_plus_memory_cache', 'path': path}
    if action == 'get':
        snapshot = _revision_cache_get(name)
        if snapshot is None:
            snapshot, error = _load_persistent_revision_baseline(name)
            if error: return {'error': error}
            if snapshot is not None: _REVISION_BASELINES[_revision_cache_key(name)] = snapshot
        if snapshot is None: return {'error': 'baseline not found'}
        return {'status': 'ok', 'name': name, 'snapshot': snapshot,
                'storage': 'persistent_file_plus_memory_cache'}
    if action == 'forget':
        path = _revision_baseline_path(name); cache_key = _revision_cache_key(name)
        existed = _revision_cache_get(name) is not None or bool(path and os.path.isfile(path))
        if not existed: return {'error': 'baseline not found'}
        _REVISION_BASELINES.pop(cache_key, None)
        if path and os.path.isfile(path):
            try: os.remove(path)
            except Exception as exc: return {'error': 'could not forget persistent baseline: %s' % exc}
        return {'status': 'ok', 'forgotten': name, 'storage': 'persistent_file_plus_memory_cache'}
    return {'error': 'action must be list, store, get, or forget'}


def get_revision_diff(p):
    baseline = p.get('baseline')
    if not baseline and p.get('baseline_name'):
        name = str(p['baseline_name'])
        baseline = _revision_cache_get(name)
        if baseline is None:
            baseline, error = _load_persistent_revision_baseline(name)
            if error: return {'error': error}
            if baseline is not None: _REVISION_BASELINES[_revision_cache_key(name)] = baseline
    current = p.get('current')
    if not isinstance(baseline, dict) or not isinstance(current, dict):
        return {'error': 'baseline and current revision snapshots are required'}
    if not _valid_revision_snapshot(baseline) or not _valid_revision_snapshot(current):
        return {'error': 'snapshot digest is invalid'}
    if baseline.get('partial') or current.get('partial'):
        return {'error': 'partial snapshots cannot be diffed'}
    if baseline.get('schema') != 1 or current.get('schema') != 1:
        return {'error': 'unsupported revision snapshot schema'}
    if baseline.get('document') != current.get('document'):
        return {'error': 'snapshots belong to different documents'}
    if baseline.get('units_per_inch') != current.get('units_per_inch'):
        return {'error': 'document units changed between snapshots'}
    if baseline.get('scope') != current.get('scope') or baseline.get('domains') != current.get('domains'):
        return {'error': 'snapshot scopes/domains differ'}
    changes = []
    for domain in baseline['domains']:
        before = {row['key']: row for row in baseline.get('rows', {}).get(domain, [])}
        after = {row['key']: row for row in current.get('rows', {}).get(domain, [])}
        for key in sorted(set(before) | set(after)):
            if key not in before:
                changes.append({'domain': domain, 'key': key, 'change': 'added',
                                'before': None, 'after': after[key]})
            elif key not in after:
                changes.append({'domain': domain, 'key': key, 'change': 'removed',
                                'before': before[key], 'after': None})
            elif before[key] != after[key]:
                fields = sorted(field for field in set(before[key]) | set(after[key])
                                if before[key].get(field) != after[key].get(field))
                changes.append({'domain': domain, 'key': key, 'change': 'changed',
                                'fields': fields, 'before': before[key], 'after': after[key]})
    summary = {'added': sum(row['change'] == 'added' for row in changes),
               'removed': sum(row['change'] == 'removed' for row in changes),
               'changed': sum(row['change'] == 'changed' for row in changes)}
    summary['total'] = len(changes); summary['clean'] = not changes
    result = {'status': 'ok', 'read_only': True,
              'baseline_digest': baseline.get('digest'),
              'current_digest': current.get('digest'),
              'summary': summary, 'changes': changes,
              'human_summary': ['%s %s: %s' % (row['change'].upper(), row['domain'], row['key'])
                                for row in changes]}
    result['diff_token'] = _revision_digest(result)
    return result


def _plan_document_changes(p):
    change_set = p.get('change_set')
    if not isinstance(change_set, dict) or not change_set:
        return None, 'change_set must be a non-empty object'
    allowed = {'object_updates', 'native_box_scopes', 'drawing_package', 'delete_ids'}
    unknown = sorted(set(change_set) - allowed)
    if unknown: return None, 'unsupported change-set sections: %s' % ', '.join(unknown)
    allow_destructive = bool(p.get('allow_destructive', False))
    updates = change_set.get('object_updates') or []
    scopes = change_set.get('native_box_scopes') or []
    delete_ids = change_set.get('delete_ids') or []
    if not isinstance(updates, list) or len(updates) > 500:
        return None, 'object_updates must be a list of at most 500 items'
    if not isinstance(scopes, list) or len(scopes) > 50:
        return None, 'native_box_scopes must be a list of at most 50 items'
    if not isinstance(delete_ids, list) or len(delete_ids) > 500:
        return None, 'delete_ids must be a list of at most 500 ids'
    if delete_ids and not allow_destructive:
        return None, 'delete_ids require allow_destructive=true'
    rows, seen, target_names = [], set(), set()
    movable = {15, 24, 25, 26, 28, 40, 84, 91}
    for index, raw in enumerate(updates):
        if not isinstance(raw, dict) or set(raw) - {'object_id', 'set'}:
            return None, 'object update %d supports only object_id/set' % index
        object_id = str(raw.get('object_id') or '').strip(); values = raw.get('set')
        if not object_id or object_id in seen or not isinstance(values, dict) or not values:
            return None, 'object updates require unique object_id and non-empty set'
        unknown_values = sorted(set(values) - {'name', 'class', 'layer', 'center'})
        if unknown_values: return None, 'unsupported object update fields: %s' % ', '.join(unknown_values)
        h = _h(object_id)
        if not h: return None, 'object_id not found: %s' % object_id
        object_type = int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0)
        center = _center3(h) if object_type in movable else None
        current = {'name': str(_safe(lambda h=h: vs.GetName(h), '') or ''),
                   'class': str(_safe(lambda h=h: vs.GetClass(h), '') or ''),
                   'layer': str(_safe(lambda h=h: vs.GetLName(vs.GetLayer(h)), '') or ''),
                   'center': center, 'type': object_type}
        target = dict(current)
        if 'name' in values:
            name = str(values['name']).strip()
            if not name or len(name) > 255: return None, 'target name must be 1..255 characters'
            collision = _safe(lambda name=name: vs.GetObject(name))
            if collision and collision != h: return None, 'target name already exists: %s' % name
            if name in target_names: return None, 'duplicate target name: %s' % name
            target_names.add(name); target['name'] = name
        if 'class' in values:
            cls = str(values['class']).strip()
            if not cls or cls not in _class_state_map(): return None, 'target class not found: %s' % cls
            target['class'] = cls
        if 'layer' in values:
            layer = str(values['layer']).strip(); lh = _safe(lambda layer=layer: vs.GetLayerByName(layer))
            if not layer or not lh: return None, 'target layer not found: %s' % layer
            target['layer'] = layer
        if 'center' in values:
            if object_type not in movable or not center:
                return None, 'object type %s cannot be safely center-moved' % object_type
            raw_center = values['center']
            if not isinstance(raw_center, dict) or not raw_center or set(raw_center) - {'x', 'y', 'z'}:
                return None, 'center must be a non-empty x/y/z object'
            try: target['center'] = {axis: float(raw_center.get(axis, center[axis])) for axis in ('x', 'y', 'z')}
            except Exception: return None, 'center values must be numeric'
            if not all(math.isfinite(value) for value in target['center'].values()):
                return None, 'center values must be finite'
        differences = [field for field in ('name', 'class', 'layer', 'center')
                       if target.get(field) != current.get(field)]
        rows.append({'object_id': object_id, 'type': object_type,
                     'current': current, 'target': target, 'differences': differences})
        seen.add(object_id)
    deleted = []
    for value in delete_ids:
        object_id = str(value or '').strip()
        if not object_id or object_id in seen: return None, 'delete ids must be unique and not updated'
        h = _h(object_id)
        if not h: return None, 'delete object_id not found: %s' % object_id
        deleted.append({**(_summary(h) or {}), 'object_id': object_id}); seen.add(object_id)
    scope_plans = []
    for index, raw in enumerate(scopes):
        if not isinstance(raw, dict): return None, 'native_box_scopes[%d] must be an object' % index
        scope = dict(raw); scope['confirm'] = False; scope.pop('plan_token', None)
        plan = reconcile_verified_native_boxes(scope)
        if plan.get('error'): return None, 'native_box_scopes[%d]: %s' % (index, plan['error'])
        summary = plan.get('summary') or {}
        if not allow_destructive and (summary.get('delete') or summary.get('replace')):
            return None, 'native box replace/delete requires allow_destructive=true'
        scope_plans.append({'params': scope, 'plan': plan})
    drawing_plan = None
    if change_set.get('drawing_package') is not None:
        package = change_set.get('drawing_package')
        drawing_plan = ensure_drawing_package({'package': package})
        if drawing_plan.get('error'): return None, 'drawing_package: %s' % drawing_plan['error']
    plan_payload = {'schema': 1, 'document': _safe(lambda: vs.GetFPathName()) or '',
                    'allow_destructive': allow_destructive, 'object_updates': rows,
                    'native_box_scopes': scope_plans, 'drawing_plan': drawing_plan,
                    'delete': deleted}
    summary = {'object_updates': sum(bool(row['differences']) for row in rows),
               'native_box_scopes': len(scope_plans),
               'drawing_changed': bool(drawing_plan and not (drawing_plan.get('summary') or {}).get('converged')),
               'deletes': len(deleted)}
    summary['converged'] = not any((summary['object_updates'], summary['drawing_changed'],
                                    summary['deletes'], any(not (row['plan'].get('summary') or {}).get('converged')
                                                            for row in scope_plans)))
    return {**plan_payload, 'status': 'dry_run', 'summary': summary,
            'plan_token': _revision_digest(plan_payload)}, None


def compile_document_changes(p):
    """Apply a token-gated staged change set across safe object, box, and drawing domains."""
    plan, error = _plan_document_changes(p)
    if error: return {'error': error}
    if not bool(p.get('confirm', False)): return plan
    if str(p.get('plan_token') or '') != plan['plan_token']:
        return {**plan, 'status': 'blocked', 'error': 'plan_token missing or stale'}
    selected_before = [_oid(h) for h in _collect('SEL=TRUE', 5001) if _oid(h)]
    if len(selected_before) > 5000: return {'error': 'selection exceeds 5000-object safety limit'}
    journal, applied = [], []

    def restore_selection():
        for h in _collect('SEL=TRUE', 5001): vs.SetDSelect(h)
        for object_id in selected_before:
            h = _h(object_id)
            if h: vs.SetSelect(h)
        return sorted(_oid(h) for h in _collect('SEL=TRUE', 5001) if _oid(h)) == sorted(selected_before)

    try:
        for row in plan['object_updates']:
            if not row['differences']: continue
            h = _h(row['object_id'])
            if not h: raise RuntimeError('object disappeared: %s' % row['object_id'])
            journal.append(row)
            target = row['target']; current = row['current']
            if 'layer' in row['differences']:
                if not bool(vs.SetParent(h, vs.GetLayerByName(target['layer']))):
                    raise RuntimeError('SetParent failed: %s' % row['object_id'])
            if 'class' in row['differences']: vs.SetClass(h, target['class'])
            if 'name' in row['differences']: vs.SetName(h, target['name'])
            if 'center' in row['differences']:
                now = _center3(h)
                vs.HMove3D(h, target['center']['x'] - now['x'],
                          target['center']['y'] - now['y'], target['center']['z'] - now['z'])
            actual = {'name': str(_safe(lambda h=h: vs.GetName(h), '') or ''),
                      'class': str(_safe(lambda h=h: vs.GetClass(h), '') or ''),
                      'layer': str(_safe(lambda h=h: vs.GetLName(vs.GetLayer(h)), '') or ''),
                      'center': _center3(h) if current.get('center') else None}
            if any(not _values_equal(target[field], actual[field], 0.01)
                   for field in row['differences']):
                raise RuntimeError('object update readback mismatch: %s' % row['object_id'])
        if journal: applied.append('object_updates')
    except Exception as exc:
        rollback_clean = True
        for row in reversed(journal):
            h = _h(row['object_id']); current = row['current']
            if not h: rollback_clean = False; continue
            _safe(lambda h=h, current=current: vs.SetParent(h, vs.GetLayerByName(current['layer'])))
            _safe(lambda h=h, current=current: vs.SetClass(h, current['class']))
            _safe(lambda h=h, current=current: vs.SetName(h, current['name']))
            if current.get('center'):
                now = _center3(h)
                _safe(lambda h=h, now=now, current=current: vs.HMove3D(
                    h, current['center']['x'] - now['x'], current['center']['y'] - now['y'],
                    current['center']['z'] - now['z']))
        for row in journal:
            h = _h(row['object_id']); current = row['current']
            if not h:
                rollback_clean = False; continue
            actual = {'name': str(_safe(lambda h=h: vs.GetName(h), '') or ''),
                      'class': str(_safe(lambda h=h: vs.GetClass(h), '') or ''),
                      'layer': str(_safe(lambda h=h: vs.GetLName(vs.GetLayer(h)), '') or ''),
                      'center': _center3(h) if current.get('center') else None}
            if any(not _values_equal(current[field], actual[field], 0.01)
                   for field in ('name', 'class', 'layer', 'center')):
                rollback_clean = False
        selection_restored = restore_selection()
        return {'status': 'error', 'error': str(exc), 'applied_stages': applied,
                'rollback_clean': rollback_clean, 'selection_restored': selection_restored}

    stage_results = []
    for index, scope_row in enumerate(plan['native_box_scopes']):
        params = dict(scope_row['params'])
        inner = reconcile_verified_native_boxes(params)
        if inner.get('error'):
            selection_restored = restore_selection()
            return {'status': 'partial', 'error': 'native_box_scope %d re-plan failed' % index,
                    'stage_result': inner, 'stage_results': stage_results,
                    'applied_stages': applied, 'selection_restored': selection_restored,
                    'rollback_scope': 'object_updates_only'}
        result = reconcile_verified_native_boxes({**params, 'confirm': True,
                                                  'plan_token': inner['plan_token']})
        stage_results.append({'stage': 'native_box_scope_%d' % index, 'result': result})
        if result.get('status') != 'ok':
            selection_restored = restore_selection()
            return {'status': 'partial', 'error': 'native_box_scope %d failed' % index,
                    'stage_result': result, 'stage_results': stage_results,
                    'applied_stages': applied, 'selection_restored': selection_restored,
                    'rollback_scope': 'object_updates_only'}
        if result.get('changed'): applied.append('native_box_scope_%d' % index)
    if plan['drawing_plan'] is not None:
        drawing_plan = ensure_drawing_package({'package': p['change_set']['drawing_package']})
        if drawing_plan.get('error'):
            selection_restored = restore_selection()
            return {'status': 'partial', 'error': 'drawing_package re-plan failed',
                    'stage_result': drawing_plan, 'stage_results': stage_results,
                    'applied_stages': applied, 'selection_restored': selection_restored,
                    'rollback_scope': 'object_updates_only'}
        result = ensure_drawing_package({
            'package': p['change_set']['drawing_package'], 'confirm': True,
            'plan_token': drawing_plan['plan_token']})
        stage_results.append({'stage': 'drawing_package', 'result': result})
        if result.get('status') != 'ok':
            selection_restored = restore_selection()
            return {'status': 'partial', 'error': 'drawing_package failed',
                    'stage_result': result, 'stage_results': stage_results,
                    'applied_stages': applied, 'selection_restored': selection_restored,
                    'rollback_scope': 'object_updates_only'}
        if result.get('changed'): applied.append('drawing_package')
    deleted = []
    for row in plan['delete']:
        h = _h(row['object_id'])
        if not h:
            restore_selection()
            return {'status': 'partial', 'error': 'delete target disappeared',
                    'deleted': deleted, 'applied_stages': applied}
        vs.DelObject(h); deleted.append(row['object_id'])
    if deleted: applied.append('delete')
    selection_restored = restore_selection()
    verify_input = p
    if deleted:
        verify_input = dict(p)
        verify_input['change_set'] = dict(p['change_set'])
        verify_input['change_set']['delete_ids'] = []
    verify, verify_error = _plan_document_changes(verify_input)
    if verify_error: return {'status': 'error', 'error': verify_error,
                             'applied_stages': applied, 'selection_restored': selection_restored}
    return {'status': 'ok', 'changed': bool(applied), 'plan_token': plan['plan_token'],
            'applied_stages': applied, 'deleted_ids': deleted,
            'selection_restored': selection_restored,
            'verification': verify, 'converged': verify['summary']['converged']}


def _viewport_dimension_rows(viewport_names=None):
    names = set(viewport_names or []); rows = []
    for viewport in _collect('T=VIEWPORT', 1000):
        viewport_name = str(_safe(lambda viewport=viewport: vs.GetName(viewport), '') or '')
        if names and viewport_name not in names: continue
        group = _safe(lambda viewport=viewport: vs.GetVPGroup(viewport, 2))
        h = _safe(lambda group=group: vs.FInGroup(group)) if group else None
        while h:
            if int(_safe(lambda h=h: vs.GetTypeN(h), 0) or 0) == 63:
                rows.append({'object_id': _oid(h), 'viewport': viewport_name,
                             'name': str(_safe(lambda h=h: vs.GetName(h), '') or ''),
                             'text': str(_safe(lambda h=h: vs.GetDimText(h), '') or '')})
            h = _safe(lambda h=h: vs.NextObj(h))
    rows.sort(key=lambda row: (row['viewport'], row['name'], row['object_id'] or ''))
    return rows


def _plan_drawing_sync(p):
    package = p.get('package') or {}
    if not isinstance(package, dict): return None, 'package must be an object'
    scopes = p.get('dimension_scopes') or []
    if not isinstance(scopes, list) or len(scopes) > 100:
        return None, 'dimension_scopes must be a list of at most 100 items'
    generated, derivations = [], []
    for index, scope in enumerate(scopes):
        if not isinstance(scope, dict): return None, 'dimension scope %d must be an object' % index
        result = plan_auto_dimensions(scope)
        if result.get('error'): return None, 'dimension scope %d: %s' % (index, result['error'])
        generated.extend((result.get('package') or {}).get('dimensions') or [])
        derivations.extend(result.get('derivations') or [])
    final_package = json.loads(json.dumps(package))
    existing_dimensions = final_package.get('dimensions') or []
    final_package['dimensions'] = existing_dimensions + generated
    names = [str(row.get('name') or '').casefold() for row in final_package['dimensions']]
    if len(names) != len(set(names)): return None, 'drawing sync produced duplicate dimension names'
    supersede = p.get('supersede_dimension_names') or []
    if isinstance(supersede, str): supersede = [supersede]
    if not isinstance(supersede, list) or len(supersede) > 500:
        return None, 'supersede_dimension_names must be a string/list of at most 500 names'
    supersede = [str(name).strip() for name in supersede]
    if any(not name for name in supersede) or len(supersede) != len(set(supersede)):
        return None, 'superseded dimension names must be unique and non-empty'
    viewport_names = sorted(set(row.get('viewport') for row in final_package['dimensions'] if row.get('viewport')))
    actual_dimensions = _viewport_dimension_rows(viewport_names)
    actual_by_name = {row['name']: row for row in actual_dimensions if row['name']}
    missing_superseded = sorted(set(supersede) - set(actual_by_name))
    if missing_superseded: return None, 'superseded dimensions not found: %s' % ', '.join(missing_superseded)
    if set(supersede).intersection(row.get('name') for row in final_package['dimensions']):
        return None, 'superseded dimensions cannot also be desired dimensions'
    package_plan = ensure_drawing_package({'package': final_package})
    if package_plan.get('error'): return None, package_plan['error']
    payload = {'schema': 1, 'document': _safe(lambda: vs.GetFPathName()) or '',
               'package': final_package, 'package_plan_token': package_plan.get('plan_token'),
               'superseded': [actual_by_name[name] for name in supersede],
               'derivations': derivations}
    return {'status': 'dry_run', 'read_only': True, **payload,
            'summary': {'desired_dimensions': len(final_package['dimensions']),
                        'superseded_dimensions': len(supersede),
                        'package': package_plan.get('summary')},
            'plan_token': _revision_digest(payload)}, None


def sync_drawing_package(p):
    """Derive dimensions, converge a drawing package, then remove explicit superseded dimensions."""
    plan, error = _plan_drawing_sync(p)
    if error: return {'error': error}
    if not bool(p.get('confirm', False)): return plan
    if str(p.get('plan_token') or '') != plan['plan_token']:
        return {**plan, 'status': 'blocked', 'error': 'plan_token missing or stale'}
    package_plan = ensure_drawing_package({'package': plan['package']})
    result = ensure_drawing_package({'package': plan['package'], 'confirm': True,
                                     'plan_token': package_plan.get('plan_token')})
    if result.get('status') != 'ok': return {'status': 'error', 'error': 'package sync failed', 'result': result}
    deleted = []
    for row in plan['superseded']:
        h = _h(row['object_id'])
        if not h or str(_safe(lambda h=h: vs.GetName(h), '') or '') != row['name']:
            return {'status': 'partial', 'error': 'superseded dimension changed before deletion',
                    'deleted': deleted, 'package_result': result}
        vs.DelObject(h); deleted.append(row['object_id'])
    verify, verify_error = _plan_drawing_sync({**p, 'supersede_dimension_names': []})
    if verify_error: return {'status': 'error', 'error': verify_error, 'deleted': deleted}
    return {'status': 'ok', 'changed': bool(result.get('changed') or deleted),
            'plan_token': plan['plan_token'], 'deleted_dimension_ids': deleted,
            'package_result': result, 'verification': verify,
            'converged': (verify.get('summary', {}).get('package') or {}).get('converged', False)}


def _normalize_drawing_set(p):
    raw = p.get('drawing_set')
    if not isinstance(raw, dict) or not raw:
        return None, 'drawing_set must be a non-empty object'
    allowed = {'sheet', 'model_layer', 'name_prefix', 'views', 'scale', 'layout',
               'visible_layers', 'classes', 'dimension_rules', 'reports', 'saved_view'}
    unknown = sorted(set(raw) - allowed)
    if unknown: return None, 'unsupported drawing_set fields: %s' % ', '.join(unknown)
    sheet = raw.get('sheet')
    if not isinstance(sheet, dict): return None, 'sheet must be an object'
    sheet_name = str(sheet.get('name') or '').strip()
    if not sheet_name: return None, 'sheet.name is required'
    model_layer = str(raw.get('model_layer') or '').strip()
    prefix = str(raw.get('name_prefix') or '').strip()
    if not model_layer or not prefix: return None, 'model_layer and name_prefix are required'
    views = raw.get('views') or ['plan', 'front', 'right', 'right_iso']
    if not isinstance(views, list) or not views or len(views) > 8:
        return None, 'views must be a non-empty list of at most 8 views'
    views = [str(view).strip().lower() for view in views]
    if len(set(views)) != len(views) or any(view not in _DRAWING_VIEW_TYPES for view in views):
        return None, 'views must be unique supported drawing views'
    try: scale = float(raw.get('scale', 48.0))
    except Exception: return None, 'scale must be numeric'
    if not math.isfinite(scale) or scale <= 0: return None, 'scale must be finite and positive'
    layout = raw.get('layout') or {}
    if not isinstance(layout, dict) or set(layout) - {'x', 'y', 'columns', 'x_spacing', 'y_spacing'}:
        return None, 'layout supports x/y/columns/x_spacing/y_spacing'
    try:
        x = float(layout.get('x', 4.0)); y = float(layout.get('y', 8.0))
        columns = int(layout.get('columns', 2))
        x_spacing = float(layout.get('x_spacing', 8.0)); y_spacing = float(layout.get('y_spacing', 6.0))
    except Exception: return None, 'layout values are invalid'
    if (not all(math.isfinite(value) for value in (x, y, x_spacing, y_spacing)) or
            columns <= 0 or x_spacing <= 0 or y_spacing <= 0):
        return None, 'layout spacing must be finite/positive and columns positive'
    visible_layers = raw.get('visible_layers') or [model_layer]
    if isinstance(visible_layers, str): visible_layers = [visible_layers]
    if not isinstance(visible_layers, list) or not visible_layers or len(visible_layers) > 100:
        return None, 'visible_layers must be a non-empty string/list'
    visible_layers = [str(name).strip() for name in visible_layers]
    if any(not name for name in visible_layers) or model_layer not in visible_layers:
        return None, 'visible_layers must include model_layer and contain no empty names'
    classes, error = _normalize_visibility_map(raw.get('classes'), 'drawing_set classes')
    if error: return None, error
    dimension_rules = raw.get('dimension_rules') or ['overall']
    if not isinstance(dimension_rules, list) or not dimension_rules:
        return None, 'dimension_rules must be a non-empty list'
    dimension_rules = [str(rule).strip().lower() for rule in dimension_rules]
    if set(dimension_rules) - {'overall', 'centers', 'members', 'contacts'}:
        return None, 'dimension_rules contain unsupported values'
    reports = raw.get('reports') or []
    saved_view = str(raw.get('saved_view') or '').strip()
    package = {
        'sheets': [{'name': sheet_name, **({'title': str(sheet['title'])} if 'title' in sheet else {}),
                    **({'dpi': sheet['dpi']} if 'dpi' in sheet else {})}],
        'viewports': [], 'reports': reports, 'dimensions': [], 'saved_views': [],
    }
    if saved_view: package['saved_views'].append({'name': saved_view})
    scopes = []
    dimension_plane = {'plan': 'plan', 'top': 'plan', 'front': 'front',
                       'left': 'side', 'right': 'side'}
    viewport_stem = prefix.rstrip('-_ ') or prefix
    for index, view in enumerate(views):
        row, column = divmod(index, columns)
        viewport_name = '%s-%s' % (viewport_stem, view.replace('_', '-').upper())
        package['viewports'].append({
            'name': viewport_name, 'sheet': sheet_name, 'view': view, 'scale': scale,
            'x': x + column * x_spacing, 'y': y - row * y_spacing,
            'layers': {name: 'visible' for name in visible_layers}, 'classes': classes,
            'hide_other_layers': True,
        })
        if view in dimension_plane:
            scopes.append({'layer': model_layer, 'name_prefix': prefix,
                           'viewport': viewport_name, 'plane': dimension_plane[view],
                           'rules': dimension_rules})
    unused_normalized_package, error = _normalize_drawing_package({'package': package})
    if error: return None, error
    return {'sheet': sheet_name, 'model_layer': model_layer, 'name_prefix': prefix,
            'views': views, 'package': package, 'dimension_scopes': scopes}, None


def _plan_drawing_set(p):
    normalized, error = _normalize_drawing_set(p)
    if error: return None, error
    package_plan = ensure_drawing_package({'package': normalized['package'], 'confirm': False})
    if package_plan.get('error'): return None, package_plan['error']
    payload = {'schema': 1, 'document': _safe(lambda: vs.GetFPathName()) or '',
               'drawing_set': normalized, 'package_plan_token': package_plan['plan_token']}
    return {'status': 'dry_run', 'read_only': True, **payload,
            'summary': {'views': len(normalized['views']),
                        'dimensioned_views': len(normalized['dimension_scopes']),
                        'package': package_plan.get('summary')},
            'plan_token': _revision_digest(payload)}, None


def compile_drawing_set(p):
    """Create/verify a standard plan drawing set from one compact specification."""
    plan, error = _plan_drawing_set(p)
    if error: return {'error': error}
    if not bool(p.get('confirm', False)): return plan
    if str(p.get('plan_token') or '') != plan['plan_token']:
        return {**plan, 'status': 'blocked', 'error': 'plan_token missing or stale'}
    selected_before, selection_error = _selected_object_ids()
    if selection_error: return {'error': 'could not snapshot selection: %s' % selection_error}
    _safe(lambda: vs.NameUndoEvent('Agent: compile plan drawing set'))
    package = plan['drawing_set']['package']
    package_plan = ensure_drawing_package({'package': package, 'confirm': False})
    if package_plan.get('error') or not package_plan.get('plan_token'):
        selection = _restore_selection_ids(selected_before)
        return {'status': 'error', 'error': 'base drawing package re-plan failed',
                'stage_result': package_plan, 'selection': selection}
    package_result = ensure_drawing_package({
        'package': package, 'confirm': True, 'plan_token': package_plan['plan_token']})
    if package_result.get('status') != 'ok':
        selection = _restore_selection_ids(selected_before)
        return {'status': 'error', 'error': 'base drawing package failed',
                'package_result': package_result, 'selection': selection}
    sync_params = {'package': package,
                   'dimension_scopes': plan['drawing_set']['dimension_scopes'],
                   'supersede_dimension_names': []}
    sync_plan = sync_drawing_package(sync_params)
    if sync_plan.get('error'):
        selection = _restore_selection_ids(selected_before)
        return {'status': 'partial', 'error': 'dimension planning failed',
                'package_result': package_result, 'stage_result': sync_plan,
                'selection': selection, 'rollback_scope': 'each inner stage only'}
    sync_result = sync_drawing_package({**sync_params, 'confirm': True,
                                        'plan_token': sync_plan['plan_token']})
    selection = _restore_selection_ids(selected_before)
    if sync_result.get('status') != 'ok' or not selection.get('restored'):
        return {'status': 'partial', 'error': ('dimension sync failed' if
                sync_result.get('status') != 'ok' else 'selection restoration failed'),
                'package_result': package_result, 'sync_result': sync_result,
                'selection': selection, 'rollback_scope': 'each inner stage only'}
    verify = sync_result.get('verification') or {}
    converged = bool(sync_result.get('converged'))
    if verify.get('error') or not converged:
        return {'status': 'partial', 'error': verify.get('error') or
                'final drawing-set verification did not converge',
                'package_result': package_result, 'sync_result': sync_result,
                'selection': selection, 'verification': verify,
                'rollback_scope': 'each inner stage only'}
    return {'status': 'ok', 'changed': bool(package_result.get('changed') or
            sync_result.get('changed')), 'plan_token': plan['plan_token'],
            'package_result': package_result, 'sync_result': sync_result,
            'selection': selection, 'verification': verify,
            'converged': converged}


def _plan_qa_autopilot(p):
    indexed = index_document({
        'layers': p.get('layers'), 'limit': p.get('index_limit', 3000),
        'guard': p.get('guard', 100000), 'max_depth': p.get('max_depth', 25),
        'record_field_limit': p.get('record_field_limit', 1000),
        'include_annotations': True, 'include_records': bool(p.get('include_records', False)),
        'include_symbols': False,
    })
    if indexed.get('error'): return None, indexed['error']
    selection_index = indexed
    if p.get('layers') is not None:
        selection_index = index_document({
            'limit': p.get('index_limit', 3000), 'guard': p.get('guard', 100000),
            'max_depth': p.get('max_depth', 25), 'record_field_limit': 0,
            'include_annotations': True, 'include_records': False, 'include_symbols': False,
        })
        if selection_index.get('error'): return None, selection_index['error']
    prefixes = p.get('temporary_prefixes') or ['MCP-', 'TMP-']
    if isinstance(prefixes, str): prefixes = [prefixes]
    if not isinstance(prefixes, list) or len(prefixes) > 20:
        return None, 'temporary_prefixes must be a string/list of at most 20 values'
    prefixes = [str(value) for value in prefixes]
    temporary = [row for row in indexed['rows'] if row['name'] and
                 any(row['name'].startswith(prefix) for prefix in prefixes)]
    selected = [row for row in selection_index['rows'] if row.get('selected')]
    expected = p.get('dimension_expectations') or []
    if not isinstance(expected, list) or len(expected) > 500:
        return None, 'dimension_expectations must be a list of at most 500 items'
    actual_dimensions = _viewport_dimension_rows(sorted(set(
        str(row.get('viewport') or '') for row in expected if row.get('viewport'))))
    dimension_findings = []
    for index, row in enumerate(expected):
        if not isinstance(row, dict) or not row.get('viewport') or not row.get('name'):
            return None, 'dimension expectation %d requires viewport/name' % index
        candidates = [actual for actual in actual_dimensions
                      if actual['viewport'] == row['viewport'] and actual['name'] == row['name']]
        state = 'missing' if not candidates else 'ok'
        if candidates and row.get('text') is not None and candidates[0]['text'] != str(row['text']): state = 'text_mismatch'
        dimension_findings.append({'expected': row, 'state': state,
                                   'actual': candidates[0] if candidates else None})
    blocking = []
    if not (_safe(lambda: vs.GetFPathName()) or ''): blocking.append('document is unsaved/unnamed')
    if indexed.get('truncated'): blocking.append('document index is truncated')
    if indexed.get('errors'): blocking.append('document index contains read errors')
    if selection_index.get('truncated'): blocking.append('global selection index is truncated')
    if selection_index.get('errors'): blocking.append('global selection index contains read errors')
    if temporary: blocking.append('temporary proof artifacts remain')
    if any(row['state'] != 'ok' for row in dimension_findings): blocking.append('dimension readback failed')
    require_clean_selection = bool(p.get('require_clean_selection', True))
    fixable = []
    if require_clean_selection and selected: fixable.append('selection is not clean')
    revision = None
    if p.get('revision_snapshot') and p.get('baseline_name'):
        supplied = p['revision_snapshot']
        if not _valid_revision_snapshot(supplied):
            blocking.append('revision snapshot is invalid')
            current_revision = supplied
        else:
            scope = supplied.get('scope') or {}
            current_revision = get_revision_snapshot({
                'domains': supplied.get('domains'), 'quantum': supplied.get('quantum', 0.001),
                'geometry_scopes': scope.get('geometry_scopes') or None,
                'layer': scope.get('layer'), 'name_prefix': scope.get('name_prefix'),
                'resource_targets': scope.get('resource_targets') or [],
                'report_names': scope.get('report_names') or [],
            })
        revision = get_revision_diff({'current': current_revision,
                                      'baseline_name': p['baseline_name']})
        if revision.get('error'): blocking.append('revision diff failed')
    payload = {'schema': 1, 'document': _safe(lambda: vs.GetFPathName()) or '',
               'index_token': indexed['index_token'],
               'selection_index_token': selection_index['index_token'],
               'temporary_ids': [row['object_id'] for row in temporary],
               'selected_ids': sorted(row['object_id'] for row in selected if row['object_id']),
               'dimension_findings': dimension_findings,
               'blocking': blocking, 'fixable': fixable,
               'baseline_name': p.get('baseline_name'),
               'revision_token': revision.get('diff_token') if isinstance(revision, dict) else None}
    return {'status': 'dry_run', 'read_only': True, **payload,
            'passed_state_checks': not blocking and not fixable,
            'visual_required': True,
            'visual_note': ('Run vwx-capture and inspect pixels; screenshots can fail QA '
                            'but never prove hidden/offscreen state complete.'),
            'revision': revision, 'plan_token': _revision_digest(payload)}, None


def qa_autopilot(p):
    """Run bounded state QA; optionally clear selection and save after token confirmation."""
    plan, error = _plan_qa_autopilot(p)
    if error: return {'error': error}
    if not bool(p.get('confirm', False)): return plan
    if str(p.get('plan_token') or '') != plan['plan_token']:
        return {**plan, 'status': 'blocked', 'error': 'plan_token missing or stale'}
    if plan['blocking']:
        return {**plan, 'status': 'blocked', 'error': 'blocking QA findings remain'}
    cleared = 0
    if bool(p.get('clear_selection', True)):
        selected_ids = plan.get('selected_ids') or []
        if len(selected_ids) > 5000: return {'error': 'selection exceeds 5000-object safety limit'}
        for object_id in selected_ids:
            h = _h(object_id)
            if not h:
                return {'status': 'error', 'error': 'selected object disappeared: %s' % object_id}
            vs.SetDSelect(h)
        cleared = len(selected_ids)
    if bool(p.get('save', False)):
        saved = save_document({})
        if saved.get('status') != 'ok': return {'status': 'error', 'error': 'save failed', 'save_result': saved}
    verify, verify_error = _plan_qa_autopilot(p)
    if verify_error: return {'status': 'error', 'error': verify_error}
    return {'status': 'ok' if verify['passed_state_checks'] else 'error',
            'changed': bool(cleared or p.get('save')), 'cleared_selection_count': cleared,
            'saved': bool(p.get('save', False)), 'verification': verify,
            'visual_required': True, 'capture_command': 'vwx-capture <output.png>'}


def _valid_revision_diff(diff):
    if not isinstance(diff, dict) or diff.get('status') != 'ok': return False
    changes = diff.get('changes')
    if not isinstance(changes, list) or not isinstance(diff.get('summary'), dict): return False
    if any(not isinstance(row, dict) or row.get('change') not in
           {'added', 'removed', 'changed'} or not row.get('domain') or not row.get('key')
           for row in changes):
        return False
    summary = diff['summary']
    if (summary.get('total') != len(changes) or
            summary.get('added') != sum(row['change'] == 'added' for row in changes) or
            summary.get('removed') != sum(row['change'] == 'removed' for row in changes) or
            summary.get('changed') != sum(row['change'] == 'changed' for row in changes)):
        return False
    payload = {key: diff[key] for key in diff if key != 'diff_token'}
    return bool(diff.get('diff_token')) and _revision_digest(payload) == diff['diff_token']


def _revision_static_text(value):
    text = str(value or '').replace('\r', ' ').replace('\n', ' ')[:500]
    probe = text.lstrip()
    return "'" + probe if probe[:1] in ('=', '+', '-', '@') else text


def ensure_revision_report(p):
    """Create, never overwrite, a static worksheet from a revision diff."""
    name = str(p.get('name') or '').strip()
    diff = p.get('diff')
    if not name or not _valid_revision_diff(diff):
        return {'error': 'name and revision diff are required'}
    if len(diff['changes']) > 500: return {'error': 'revision report exceeds 500 changes'}
    existing = _safe(lambda: vs.GetObject(name))
    state = 'collision' if existing else 'create'
    payload = {'document': _safe(lambda: vs.GetFPathName()) or '', 'name': name,
               'diff_token': diff.get('diff_token'), 'changes': diff['changes'],
               'existing_type': int(_safe(lambda: vs.GetTypeN(existing), 0) or 0) if existing else 0}
    token = _revision_digest(payload)
    base = {'status': 'dry_run', 'plan_token': token, 'name': name, 'state': state,
            'row_count': len(diff['changes']) + 2}
    if not bool(p.get('confirm', False)): return base
    if str(p.get('plan_token') or '') != token:
        return {**base, 'status': 'blocked', 'error': 'plan_token missing or stale'}
    if existing:
        return {**base, 'status': 'blocked', 'error': 'worksheet name already exists'}
    ws = None
    try:
        ws = vs.CreateWS(name, len(diff['changes']) + 2, 5)
        if not ws: raise RuntimeError('CreateWS failed')
        headers = ['Change', 'Domain', 'Key', 'Fields', 'Summary']
        for col, value in enumerate(headers, 1):
            vs.SetWSCellFormula(ws, 1, col, 1, col, _revision_static_text(value))
        summary_text = 'Baseline %s -> Current %s' % (
            diff.get('baseline_digest', ''), diff.get('current_digest', ''))
        vs.SetWSCellFormula(ws, 2, 1, 2, 1, _revision_static_text(summary_text))
        for row_num, change in enumerate(diff['changes'], 3):
            values = [change.get('change', ''), change.get('domain', ''),
                      change.get('key', ''), ', '.join(change.get('fields') or []),
                      '%s %s' % (change.get('change', '').upper(), change.get('key', ''))]
            for col, value in enumerate(values, 1):
                vs.SetWSCellFormula(ws, row_num, col, row_num, col,
                                    _revision_static_text(value))
        size = _safe(lambda: vs.GetWSRowColumnCount(ws))
        if not isinstance(size, (list, tuple)) or tuple(size[:2]) != (len(diff['changes']) + 2, 5):
            raise RuntimeError('revision report readback failed')
        if str(_safe(lambda: vs.GetWSCellString(ws, 1, 1), '') or '') != 'Change':
            raise RuntimeError('revision report header readback failed')
        if diff['changes']:
            first = diff['changes'][0]
            expected = _revision_static_text(first.get('change', '')).lstrip("'")
            if str(_safe(lambda: vs.GetWSCellString(ws, 3, 1), '') or '') != expected:
                raise RuntimeError('revision report data readback failed')
            expected_key = _revision_static_text(first.get('key', ''))
            if str(_safe(lambda: vs.GetWSCellString(ws, 3, 3), '') or '') != expected_key:
                raise RuntimeError('revision report key readback failed')
        return {'status': 'ok', 'changed': True, 'plan_token': token,
                'name': name, 'rows': size[0], 'columns': size[1]}
    except Exception as exc:
        if ws: _safe(lambda ws=ws: vs.DelObject(ws))
        return {'status': 'error', 'error': str(exc),
                'rollback_clean': not bool(_safe(lambda: vs.GetObject(name)))}

def _res_walk(fld, depth, acc, types):
    k = vs.FInFolder(fld)
    while k:
        t = vs.GetTypeN(k)
        nm = vs.GetName(k) or ''
        if t == 92:
            acc.append({'name': nm, 'type': 'folder', 'depth': depth})
            _res_walk(k, depth + 1, acc, types)
        elif not types or t in types:
            acc.append({'name': nm, 'type': t, 'depth': depth})
        k = vs.NextObj(k)

def resource_tree(p):
    """List the resource tree (folders + symbol defs / Marionette styles).
    params: root (optional folder name to start from), symbols_only (bool)."""
    acc = []
    types = [16] if p.get('symbols_only') else None
    if p.get('root'):
        fld = vs.GetObject(p['root'])
        if not fld or vs.GetTypeN(fld) != 92:
            return {'error': 'folder not found: %s' % p['root']}
        _res_walk(fld, 0, acc, types)
    else:
        sd = vs.FSymDef()
        while sd:
            t = vs.GetTypeN(sd)
            nm = vs.GetName(sd) or ''
            if t == 92:
                acc.append({'name': nm, 'type': 'folder', 'depth': 0})
                _res_walk(sd, 1, acc, types)
            elif not types or t in types:
                acc.append({'name': nm, 'type': t, 'depth': 0})
            sd = vs.NextSymDef(sd)
    return {'status': 'ok', 'items': acc, 'count': len(acc)}

def resource_move(p):
    """Move a resource (symbol/style/folder) into a folder via vs.SetParent.
    params: name, folder (target folder name)."""
    r = _safe(lambda: vs.GetObject(p.get('name', '')))
    fld = _safe(lambda: vs.GetObject(p.get('folder', '')))
    if not r: return {'error': 'resource not found: %s' % p.get('name')}
    if not fld or vs.GetTypeN(fld) != 92:
        return {'error': 'target folder not found: %s' % p.get('folder')}
    ok = vs.SetParent(r, fld)
    return {'status': 'ok' if ok else 'failed', 'moved': bool(ok)}

def resource_rename(p):
    """Rename a resource. Names are GLOBAL (shared with drawing objects) —
    collisions fail silently, so the result is verified. params: name, new_name."""
    r = _safe(lambda: vs.GetObject(p.get('name', '')))
    if not r: return {'error': 'resource not found: %s' % p.get('name')}
    if _safe(lambda: vs.GetObject(p.get('new_name', ''))):
        return {'error': 'name already in use (global namespace): %s' % p.get('new_name')}
    vs.SetName(r, p.get('new_name', ''))
    return {'status': 'ok', 'renamed': vs.GetName(r) == p.get('new_name')}

def resource_delete(p):
    """Delete a resource. WARNING: deleting a Marionette style deletes bound
    instances — set unbind_instances=True (default) to unbind them first.
    params: name, unbind_instances (bool, default True)."""
    r = _safe(lambda: vs.GetObject(p.get('name', '')))
    if not r: return {'error': 'resource not found: %s' % p.get('name')}
    unbound = 0
    if p.get('unbind_instances', True) and vs.GetTypeN(r) == 16:
        objs = []
        def cb(h): objs.append(h)
        _safe(lambda: vs.ForEachObject(cb, "(T=PLUGINOBJECT)"))
        for h in objs:
            if _safe(lambda: vs.GetPluginStyle(h)) == (vs.GetName(r) or ''):
                _safe(lambda: vs.SetPluginStyle(h, ''))
                unbound += 1
    vs.DelObject(r)
    return {'status': 'ok', 'unbound_instances': unbound}

def resource_create_folder(p):
    """Try to create a symbol folder by script. UNRELIABLE on VW2026 (see block
    header) — probes BeginFolder/BeginFolderN and verifies via FSymDef diff.
    If it fails, create the folder once in the Zubehör-Manager GUI and use
    resource_move / resource_rename. params: name, parent (optional folder)."""
    name = p.get('name')
    if not name: return {'error': 'name required'}
    if _safe(lambda: vs.GetObject(name)):
        return {'error': 'name already in use: %s' % name}
    def snap():
        s = set()
        sd = vs.FSymDef()
        while sd:
            s.add(vs.GetObjectUuid(sd))
            sd = vs.NextSymDef(sd)
        return s
    before = snap()
    attempts = [lambda: vs.BeginFolder(), lambda: vs.BeginFolderN(1),
                lambda: vs.BeginFolderN(2)]
    for att in attempts:
        try:
            att(); vs.EndFolder()
        except Exception:
            continue
        new = snap() - before
        if new:
            h = vs.GetObjectByUuid(list(new)[0])
            if vs.GetTypeN(h) == 92:
                vs.SetName(h, name)
                if p.get('parent'):
                    tgt = vs.GetObject(p['parent'])
                    if tgt and vs.GetTypeN(tgt) == 92:
                        vs.SetParent(h, tgt)
                return {'status': 'ok', 'object_id': _oid(h)}
            vs.DelObject(h)
    return {'error': 'folder creation failed (known VW2026 limitation) — '
                     'create it once in the Zubehör-Manager GUI, then use '
                     'resource_move/resource_rename'}

def marionette_style_clone(p):
    """Clone a Marionette object style and set its parameters — the proven bulk
    path for style libraries. params: template (style name), new_name,
    folder (optional target), params (dict: OIP label -> value; labels matched
    with numeric prefixes stripped), node_params (dict varName -> value applied
    to a node type given in node_type, default 'WinkelWand')."""
    import json as _json
    tpl = _safe(lambda: vs.GetObject(p.get('template', '')))
    if not tpl or vs.GetTypeN(tpl) != 16:
        return {'error': 'template style not found: %s' % p.get('template')}
    if _safe(lambda: vs.GetObject(p.get('new_name', ''))):
        return {'error': 'name already in use: %s' % p.get('new_name')}
    c = vs.HDuplicate(tpl, 0, 0)
    if not c: return {'error': 'HDuplicate failed'}
    vs.SetName(c, p.get('new_name', ''))
    params = p.get('params') or {}
    node_params = p.get('node_params') or {}
    node_type = p.get('node_type', 'WinkelWand')
    def strip(s):
        return (s or '').lstrip('0123456789 ').strip()
    w = vs.FInSymDef(c) if hasattr(vs, 'FInSymDef') else vs.FIn3D(c)
    while w:
        if _safe(lambda: vs.GetRField(w, 'MarionetteObject2D', 'NodeType')) == 'Wrapper':
            if params:
                d = _json.loads(vs.GetRField(w, 'MarionetteObject2D', 'NodeDef_OIPControls'))
                for cc in d['data']:
                    key = strip(cc.get('text'))
                    if key in params: cc['value'] = params[key]
                vs.SetRField(w, 'MarionetteObject2D', 'NodeDef_OIPControls', _json.dumps(d))
            grp = vs.FIn3D(w)
            ch = vs.FIn3D(grp) if grp else 0
            while ch:
                nm = strip(_safe(lambda: vs.GetRField(ch, 'MarionetteNode', 'NodeName')))
                nt = _safe(lambda: vs.GetRField(ch, 'MarionetteNode', 'NodeType'))
                if nm in params:
                    dd = _json.loads(vs.GetRField(ch, 'MarionetteNode', 'NodeDef_OIPControls'))
                    if dd.get('data'):
                        dd['data'][0]['value'] = params[nm]
                        vs.SetRField(ch, 'MarionetteNode', 'NodeDef_OIPControls', _json.dumps(dd))
                if node_params and nt == node_type:
                    dd = _json.loads(vs.GetRField(ch, 'MarionetteNode', 'NodeDef_OIPControls'))
                    for cc in dd['data']:
                        if cc['varName'] in node_params: cc['value'] = node_params[cc['varName']]
                    vs.SetRField(ch, 'MarionetteNode', 'NodeDef_OIPControls', _json.dumps(dd))
                    vs.SetRField(ch, 'MarionetteNode', 'LinkedObj', '')
                ch = vs.NextObj(ch)
        w = vs.NextObj(w)
    if p.get('folder'):
        fld = vs.GetObject(p['folder'])
        if fld and vs.GetTypeN(fld) == 92:
            vs.SetParent(c, fld)
    return {'status': 'ok', 'object_id': _oid(c)}

def get_object_style(p):
    """Get the Marionette/plugin style bound to an object. params: object_id."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    return {'status': 'ok', 'style': _safe(lambda: vs.GetPluginStyle(h), '')}

def set_object_style(p):
    """Bind or unbind a plugin style. params: object_id, style ('' = unbind).
    NOTE: binding moves the wrapper's inner network into the style; unbinding
    does NOT restore it — rebuild instances from scratch if needed."""
    h = _h(p.get('object_id'))
    if not h: return {'error': 'Object not found'}
    r = vs.SetPluginStyle(h, p.get('style', ''))
    return {'status': 'ok', 'result': str(r), 'style_now': _safe(lambda: vs.GetPluginStyle(h), '')}

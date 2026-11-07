"""Checks every Fusion API member the add-in uses against the definitions shipped with Fusion.

Skipped when Fusion is not installed. This catches renamed or removed members, which is what broke
the old `currentSelection.size()` call. It cannot prove that events fire: only a run inside Fusion can.
"""
import ast, glob, os, unittest, warnings

DEFS = glob.glob(os.path.expanduser(
    '~/Library/Application Support/Autodesk/webdeploy/production/*/Autodesk Fusion.app/Contents/Api/Python/packages/adsk/defs/adsk'))
if os.environ.get('FUSION_API_DEFS'):
    DEFS = [os.environ['FUSION_API_DEFS']]

USED = {
    'core': {
        'Application': ['get', 'userInterface', 'version', 'activeProduct', 'activeDocument', 'activeEditObject',
                        'documentActivating', 'documentSaved', 'documentClosing', 'cameraChanged',
                        'registerCustomEvent', 'unregisterCustomEvent', 'fireCustomEvent'],
        'UserInterface': ['commandStarting', 'commandTerminated', 'activeSelectionChanged', 'markingMenuDisplaying',
                          'activeWorkspace', 'palettes', 'commandDefinitions', 'allToolbarPanels', 'allToolbarTabs', 'messageBox'],
        'ApplicationCommandEventArgs': ['commandId', 'isCanceled', 'terminationReason', 'cast'],
        'ActiveSelectionEventArgs': ['currentSelection', 'cast'],
        'MarkingMenuEventArgs': ['selectedEntities', 'cast'],
        'CameraEventArgs': ['viewport', 'cast'],
        'DocumentEventArgs': ['document', 'cast'],
        'HTMLEventArgs': ['action', 'data', 'returnData', 'cast'],
        'Selection': ['entity', 'point'],
        'Document': ['name', 'isModified'],
        'Product': ['productType'],
        'Base': ['objectType'],
        'Viewport': ['camera'],
        'Camera': ['viewOrientation', 'upVector', 'target'],
        'Workspace': ['id'],
        'BoundingBox3D': ['minPoint', 'maxPoint'],
        'Point3D': ['asArray'],
        'Vector3D': ['asArray'],
        'Palettes': ['add', 'itemById'],
        'Palette': ['dockingState', 'incomingFromHTML', 'closed', 'isVisible', 'deleteMe', 'sendInfoToHTML'],
        'CustomEvent': ['add', 'remove'],
        'CommandDefinitions': ['itemById', 'addButtonDefinition'],
        'CommandDefinition': ['commandCreated', 'execute', 'deleteMe', 'name', 'tooltip', 'resourceFolder'],
        'CommandControl': ['isPromoted'],
        'CommandCreatedEventArgs': ['command'],
        'Command': ['execute'],
        'ToolbarPanelList': ['itemById'],
        'ToolbarTabList': ['count', 'item'],
        'ToolbarTab': ['id', 'toolbarPanels'],
        'ToolbarPanels': ['count', 'item'],
        'ToolbarPanel': ['id', 'controls'],
        'ToolbarControls': ['count', 'item', 'itemById', 'addCommand'],
        'ToolbarControl': ['id', 'deleteMe'],
        'DropDownControl': ['controls'],
    },
    'fusion': {
        'Design': ['cast', 'activeComponent', 'rootComponent', 'timeline', 'designType'],
        'Component': ['id', 'occurrences', 'getPhysicalProperties'],
        'PhysicalProperties': ['volume', 'area', 'density', 'centerOfMass', 'getXYZMomentsOfInertia'],
        'Occurrences': ['count'],
        'Timeline': ['count', 'item', 'markerPosition'],
        'TimelineObject': ['healthState'],
        'BRepFace': ['area', 'boundingBox', 'entityToken'],
        'BRepEdge': ['length', 'boundingBox', 'entityToken'],
    },
}
ENUMS = {
    ('core', 'CommandTerminationReason'): {'UnknownTerminationReason': 0, 'CompletedTerminationReason': 1,
                                           'CancelledTerminationReason': 2, 'AbortedTerminationReason': 3,
                                           'PreEmptedTerminationReason': 4, 'SessionEndingTerminationReason': 5},
    ('fusion', 'FeatureHealthStates'): {'WarningFeatureHealthState': 1, 'ErrorFeatureHealthState': 2},
    ('core', 'PaletteDockingStates'): {'PaletteDockStateRight': None},
    ('fusion', 'CalculationAccuracy'): {'VeryHighCalculationAccuracy': 3},
}
HANDLERS = ['CameraEventHandler', 'MarkingMenuEventHandler', 'ActiveSelectionEventHandler', 'ApplicationCommandEventHandler',
            'DocumentEventHandler', 'CommandEventHandler', 'CommandCreatedEventHandler',
            'UserInterfaceGeneralEventHandler', 'HTMLEventHandler', 'CustomEventHandler']


def load_classes(defs, module):
    with open(os.path.join(defs, module + '.py'), encoding='utf-8') as f:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            tree = ast.parse(f.read())
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def members(classes, name):
    found, values = set(), {}
    node = classes.get(name)
    if node is None:
        return found, values
    for item in node.body:
        if isinstance(item, ast.FunctionDef):
            found.add(item.name)
        elif isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant):
            for target in item.targets:
                found.add(target.name if hasattr(target, 'name') else target.id)
                values[target.id] = item.value.value
    for base in node.bases:
        base_name = base.attr if isinstance(base, ast.Attribute) else getattr(base, 'id', None)
        inherited, inherited_values = members(classes, base_name)
        found |= inherited
        values.update(inherited_values)
    return found, values


@unittest.skipUnless(DEFS, 'Fusion is not installed')
class ApiSurface(unittest.TestCase):
    def setUp(self):
        self.classes = {m: load_classes(DEFS[0], m) for m in ['core', 'fusion']}
        # fusion classes inherit from core ones, for example Design from Product.
        self.classes['fusion'] = {**self.classes['core'], **self.classes['fusion']}

    def test_members_exist(self):
        missing = []
        for module, used in USED.items():
            for class_name, names in used.items():
                found, _ = members(self.classes[module], class_name)
                missing += [f'{class_name}.{n}' for n in names if n not in found]
        self.assertEqual(missing, [])

    def test_enum_values_match(self):
        for (module, name), expected in ENUMS.items():
            found, values = members(self.classes[module], name)
            for member, value in expected.items():
                self.assertIn(member, found)
                if value is not None:
                    self.assertEqual(values[member], value)

    def test_handler_bases_exist(self):
        for name in HANDLERS:
            self.assertIn(name, self.classes['core'])

    def test_palette_add_accepts_browser_choice(self):
        add = next(n for n in self.classes['core']['Palettes'].body if getattr(n, 'name', '') == 'add')
        self.assertIn('useNewWebBrowser', [a.arg for a in add.args.args])

    def test_selection_results_are_lists(self):
        source = ast.unparse(self.classes['core']['ActiveSelectionEventArgs'])
        self.assertIn('list[Selection]', source)


if __name__ == '__main__':
    unittest.main()

"""A stand-in for the adsk modules, which only exist inside Fusion.

test_api_surface.py checks the members used here against Fusion's own API definitions, so that
this fake cannot drift into approving calls the real API does not have.
"""
import sys, types


class Event:
    def __init__(self):
        self.listeners = []

    def add(self, handler):
        self.listeners.append(handler)
        return True

    def remove(self, handler):
        self.listeners.remove(handler)
        return True

    def fire(self, args):
        for listener in list(self.listeners):
            listener.notify(args)


class Handler:
    def __init__(self):
        pass


class Castable:
    @staticmethod
    def cast(arg):
        return arg


class Point:
    def __init__(self, *values):
        self.values = values

    def asArray(self):
        return self.values


class Box:
    minPoint = Point(0.0, 0.0, 0.0)
    maxPoint = Point(1.23456, 2.0, 3.0)


class Face:
    objectType = 'adsk::fusion::BRepFace'
    entityToken = 'secret-token'
    area = 12.345678
    boundingBox = Box()
    name = 'SECRET-FEATURE-NAME'

    @property
    def length(self):
        raise AttributeError('faces have no length')


class Selection:
    def __init__(self, entity=None):
        self.entity = entity or Face()
        self.point = Point(1.000049, 2.0, 3.0)


class Args:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class PhysicalProperties:
    def __init__(self, volume=10.0, area=20.0, center=(1.0, 2.0, 3.0), moments=(4.0, 5.0, 6.0, 0.0, 0.0, 0.0), density=2.0):
        self.volume, self.area, self.density = volume, area, density
        self.centerOfMass = Point(*center)
        self.moments = moments

    def getXYZMomentsOfInertia(self):
        return (True, *self.moments)


class Component:
    def __init__(self, component_id, occurrence_count=0):
        self.id = component_id
        self.occurrences = Args(count=occurrence_count)
        self.props = PhysicalProperties()
        self.accuracy_asked = None

    def getPhysicalProperties(self, accuracy=0):
        self.accuracy_asked = accuracy
        return self.props


class TimelineItem:
    def __init__(self, state=0):
        self.healthState = state


class Timeline:
    def __init__(self):
        self.items = [TimelineItem(), TimelineItem(1), TimelineItem(2)]
        self.markerPosition = 3

    @property
    def count(self):
        return len(self.items)

    def item(self, index):
        return self.items[index]


class Design:
    productType = 'DesignProductType'
    designType = 1

    def __init__(self):
        self.rootComponent = Component('root')
        self.activeComponent = self.rootComponent
        self.timeline = Timeline()

    @staticmethod
    def cast(arg):
        return arg if isinstance(arg, Design) else None


class Control:
    def __init__(self, control_id):
        self.id = control_id
        self.isPromoted = False
        self.deleted = False

    def deleteMe(self):
        self.deleted = True


class Controls:
    def __init__(self, controls=()):
        self.controls = list(controls)

    @property
    def count(self):
        return len(self.controls)

    def item(self, index):
        return self.controls[index]

    def itemById(self, control_id):
        for control in self.controls:
            if control.id == control_id and not control.deleted:
                return control
        return None

    def addCommand(self, definition):
        control = Control(definition.id)
        self.controls.append(control)
        return control


class Panel:
    def __init__(self, panel_id, controls=()):
        self.id = panel_id
        self.controls = Controls(controls)


class Collection:
    def __init__(self, items):
        self.items = items

    @property
    def count(self):
        return len(self.items)

    def item(self, index):
        return self.items[index]


class Panels:
    def __init__(self, panel):
        self.panel = panel

    def itemById(self, panel_id):
        return self.panel if panel_id == self.panel.id else None


class CommandDefinition:
    def __init__(self, definition_id, owner):
        self.id = definition_id
        self.owner = owner
        self.commandCreated = Event()

    def execute(self):
        command = Args(execute=Event())
        self.commandCreated.fire(Args(command=command))
        command.execute.fire(Args())

    def deleteMe(self):
        self.owner.definitions.pop(self.id, None)


class CommandDefinitions:
    def __init__(self):
        self.definitions = {}

    def itemById(self, definition_id):
        return self.definitions.get(definition_id)

    def addButtonDefinition(self, definition_id, name, tooltip, resources):
        definition = CommandDefinition(definition_id, self)
        definition.name, definition.tooltip, definition.resourceFolder = name, tooltip, resources
        self.definitions[definition_id] = definition
        return definition


class Palette:
    def __init__(self, palette_id, url):
        self.id = palette_id
        self.htmlFileURL = url
        self.isVisible = True
        self.dockingState = None
        self.incomingFromHTML = Event()
        self.closed = Event()
        self.deleted = False
        self.sent = []

    def sendInfoToHTML(self, action, data):
        self.sent.append((action, data))
        return True

    def deleteMe(self):
        self.deleted = True


class Palettes:
    def __init__(self):
        self.palettes = {}

    def itemById(self, palette_id):
        palette = self.palettes.get(palette_id)
        return palette if palette and not palette.deleted else None

    def add(self, palette_id, name, url, is_visible, close_button, resizable, width, height, useNewWebBrowser=False):
        self.palettes[palette_id] = Palette(palette_id, url)
        self.palettes[palette_id].uses_new_browser = useNewWebBrowser
        self.palettes[palette_id].has_close_button = close_button
        return self.palettes[palette_id]


class UserInterface:
    def __init__(self):
        self.commandStarting = Event()
        self.commandTerminated = Event()
        self.activeSelectionChanged = Event()
        self.markingMenuDisplaying = Event()
        self.activeWorkspace = Args(id='FusionSolidEnvironment')
        self.palettes = Palettes()
        self.commandDefinitions = CommandDefinitions()
        self.allToolbarPanels = Panels(Panel('SolidScriptsAddinsPanel'))
        inner = Panel('SolidCreatePanel', [Control('ExtrudeCommand')])
        self.allToolbarTabs = Collection([Args(id='SolidTab', toolbarPanels=Collection([inner]))])
        self.messages = []

    def messageBox(self, text):
        self.messages.append(text)


class Application:
    current = None

    def __init__(self):
        self.userInterface = UserInterface()
        self.documentActivating = Event()
        self.documentSaved = Event()
        self.documentClosing = Event()
        self.cameraChanged = Event()
        self.userName = 'SECRET-USERNAME'
        self.userId = 'SECRET-USERID'
        self.version = '2705.1.15'
        self.activeProduct = Design()
        self.activeDocument = Args(name='SECRET-DOCUMENT|with\nawkward chars v3')
        self.activeEditObject = Args(objectType='adsk::fusion::Component', name='SECRET-EDIT-OBJECT')
        self.customEvents = {}
        self.queued = []

    def registerCustomEvent(self, event_id):
        # Fusion returns null for an id that is already registered.
        if event_id in self.customEvents:
            return None
        self.customEvents[event_id] = Event()
        return self.customEvents[event_id]

    def unregisterCustomEvent(self, event_id):
        return self.customEvents.pop(event_id, None) is not None

    def fireCustomEvent(self, event_id, additionalInfo=''):
        # Queued, as in Fusion. runQueued stands in for Fusion going idle on the main thread.
        self.queued.append(event_id)
        return event_id in self.customEvents

    def runQueued(self):
        while self.queued:
            event = self.customEvents.get(self.queued.pop(0))
            if event:
                event.fire(Args(additionalInfo=''))

    @staticmethod
    def get():
        return Application.current

    @staticmethod
    def cast(arg):
        return arg


def install():
    Application.current = Application()
    adsk = types.ModuleType('adsk')
    core = types.ModuleType('adsk.core')
    fusion = types.ModuleType('adsk.fusion')
    core.Application = Application
    for name in ['CameraEventHandler', 'MarkingMenuEventHandler', 'ActiveSelectionEventHandler',
                 'ApplicationCommandEventHandler', 'DocumentEventHandler', 'CommandEventHandler',
                 'CommandCreatedEventHandler', 'UserInterfaceGeneralEventHandler', 'HTMLEventHandler', 'CustomEventHandler']:
        setattr(core, name, Handler)
    for name in ['ApplicationCommandEventArgs', 'ActiveSelectionEventArgs', 'MarkingMenuEventArgs',
                 'CameraEventArgs', 'DocumentEventArgs', 'HTMLEventArgs', 'CustomEventArgs']:
        setattr(core, name, Castable)
    core.PaletteDockingStates = Args(PaletteDockStateRight=1)
    fusion.Design = Design
    fusion.CalculationAccuracy = Args(VeryHighCalculationAccuracy=3)
    fusion.FeatureHealthStates = Args(HealthyFeatureHealthState=0, WarningFeatureHealthState=1, ErrorFeatureHealthState=2)
    adsk.core = core
    adsk.fusion = fusion
    sys.modules.update({'adsk': adsk, 'adsk.core': core, 'adsk.fusion': fusion})
    return Application.current

from __future__ import absolute_import, division, print_function, unicode_literals
import os
from PySide.QtCore import Qt

from arrow import arrow
from PySide import QtGui, QtCore
from mcedit2.appsettings import RecentFilesSetting
from mcedit2.rendering import blockmeshes
from mcedit2.rendering.blockmodels import BlockModels
from mcedit2.rendering.chunkloader import ChunkLoader
from mcedit2.rendering.textureatlas import TextureAtlas
from mcedit2.util import profiler, minecraftinstall
from mcedit2.util.load_ui import load_ui
from mcedit2.util.minecraftinstall import MinecraftInstallsDialog
from mcedit2.util.screen import centerWidgetInScreen
from mcedit2.util.worldloader import LoaderTimer

from mcedit2.widgets.layout import Column, Row, setWidgetError
from mcedit2.worldview.minimap import MinimapWorldView
from mceditlib.anvil.adapter import AnvilWorldAdapter
from mceditlib.geometry import Vector
from mceditlib.exceptions import LevelFormatError, PlayerNotFound
from mceditlib import worldeditor

import logging
from mceditlib.findadapter import isLevel, findAdapter
from mceditlib.util import displayName

log = logging.getLogger(__name__)


def lastPlayedTime(adapter):
    try:
        time = adapter.metadata.LastPlayed
        dt = arrow.Arrow.fromtimestamp(time / 1000.0)
        return dt
    except AttributeError as e:
        return None


def usefulFilename(adapter):
    if hasattr(adapter, 'worldFolder'):
        return os.path.basename(adapter.worldFolder.filename)
    else:
        return os.path.basename(adapter.filename)


class WorldListItemWidget(QtGui.QWidget):
    doubleClicked = QtCore.Signal()

    def __init__(self, parent=None):
        QtGui.QWidget.__init__(self, parent)

        self.displayNameLabel = QtGui.QLabel("namenamename")
        self.lastPlayedLabel = QtGui.QLabel("lastplayed")

        #self.sizeLabel = QtGui.QLabel(self.tr("Calculating area..."))
        # areaText = self.tr("%.02f million square meters") % (world.chunkCount * 0.25)
        # diskSize = 0
        # if hasattr(worldAdapter, 'worldFolder'):
        #     folder = worldAdapter.worldFolder
        #     for rf in folder.findRegionFiles():
        #         diskSize += os.stat(rf).st_size
        # else:
        #     diskSize = os.stat(worldAdapter.filename).st_size
        #
        # self.diskSizeLabel = QtGui.QLabel(self.tr("%0.2f MB") % (diskSize / 1000000.0))

        infoColumn = Column(
            self.displayNameLabel,
            self.lastPlayedLabel,
            #self.diskSizeLabel,
            None
        )

        layout = Row(20, infoColumn)

        self.setLayout(layout)

    def setWorldInfo(self, (name, lastPlayedText)):
        self.displayNameLabel.setText(name)
        self.lastPlayedLabel.setText(lastPlayedText)

    def mouseDoubleClickEvent(self, event):
        self.doubleClicked.emit()

    def setErrorMessage(self, msg):
        self.sizeLabel.setText(msg)


def getWorldInfo(filename):
    worldAdapter = findAdapter(filename, readonly=True)
    try:
        displayNameLimit = 40
        name = displayName(worldAdapter.filename)

        if len(name) > displayNameLimit:
            name = name[:displayNameLimit] + "..."
        if usefulFilename(worldAdapter) != displayName(worldAdapter.filename):
            name = "%s (%s)" % (name, usefulFilename(worldAdapter))

        lastPlayed = lastPlayedTime(worldAdapter)
        lastPlayedText = lastPlayed.humanize() if lastPlayed else "Unknown"
        return name, lastPlayedText
    except EnvironmentError as e:
        log.error("Failed getting world info for %s: %s", filename, e)
        return str(e), ""


class WorldListItemDelegate(QtGui.QStyledItemDelegate):
    def __init__(self):
        super(WorldListItemDelegate, self).__init__()
        self.itemWidget = WorldListItemWidget()
        self.itemWidget.adjustSize()
        log.info("Size hint: %s", str(self.itemWidget.sizeHint()))
        log.info("Size : %s", str(self.itemWidget.size()))

    def paint(self, painter, option, index):
        """

        :param painter:
        :type painter: QtGui.QPainter
        :param option:
        :type option: QtGui.QStyleOptionViewItemV4
        :param index:
        :type index:
        :return:
        :rtype:
        """
        option = QtGui.QStyleOptionViewItemV4(option)
        self.initStyleOption(option, index)
        style = QtGui.qApp.style()
        worldInfo = index.data(Qt.UserRole)
        style.drawPrimitive(QtGui.QStyle.PE_PanelItemViewItem, option, painter, self.parent())
        self.itemWidget.setGeometry(option.rect)
        self.itemWidget.setWorldInfo(worldInfo)
        self.itemWidget.render(painter,
                               painter.deviceTransform().map(option.rect.topLeft()),  # QTBUG-26694
                               renderFlags=QtGui.QWidget.DrawChildren)

    def sizeHint(self, option, index):
        return self.itemWidget.sizeHint()


class WorldListModel(QtCore.QAbstractListModel):
    def __init__(self, worlds=None):
        super(WorldListModel, self).__init__()
        if worlds is None:
            worlds = []

        self.worlds = [(f, getWorldInfo(f)) for f in worlds]

    def rowCount(self, index):
        if index.isValid():
            return 0

        return len(self.worlds)

    def data(self, index, role=Qt.DisplayRole):
        if index.column() != 0:
            return
        row = index.row()

        if role == Qt.DisplayRole:
            return self.worlds[row][0]
        if role == Qt.UserRole:
            return self.worlds[row][1]

    def flags(self, index):
        if not index.isValid():
            return 0
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

class WorldListWidget(QtGui.QDialog):
    def __init__(self, parent=None, f=0):
        super(WorldListWidget, self).__init__(parent, f)
        self.setWindowTitle("World List")

        self.saveFileDir = None
        self.worldView = None
        self.chunkLoader = None

        self.errorWidget = QtGui.QWidget()

        load_ui('world_list.ui', baseinstance=self)

        self.setLayout(Row(self))

        self.editButton.clicked.connect(self.editClicked)
        self.cancelButton.clicked.connect(self.reject)
        self.showListAgainInput.setEnabled(False)

        self.viewButton.clicked.connect(self.viewClicked)
        self.viewButton.setEnabled(False)

        self.openWorldButton.clicked.connect(self.openWorldClicked)

        self.repairButton.clicked.connect(self.repairClicked)
        self.repairButton.setEnabled(False)
        self.backupButton.clicked.connect(self.backupClicked)
        self.backupButton.setEnabled(False)
        self.configureButton.clicked.connect(self.configureClicked)

        centerWidgetInScreen(self, 0.75)

        delegate = WorldListItemDelegate()
        self.worldListView.setItemDelegate(delegate)
        delegate.setParent(self.worldListView)  # PYSIDE-152: get the view widget to the drawPrimitive call

        self.worldListView.clicked.connect(self.worldListItemClicked)
        self.worldListView.doubleClicked.connect(self.worldListItemDoubleClicked)

        self.loadTimer = LoaderTimer(interval=0, timeout=self.loadTimerFired)
        self.loadTimer.start()

        for install in minecraftinstall.listInstalls():
            self.minecraftInstallBox.addItem(install.name)
        self.minecraftInstallBox.setCurrentIndex(minecraftinstall.selectedInstallIndex())
        self._updateVersionsAndResourcePacks()

        self.worldListModel = None
        self.reloadList()

    def _updateVersionsAndResourcePacks(self):
        install = minecraftinstall.getInstall(self.minecraftInstallBox.currentIndex())
        for version in sorted(install.versions, reverse=True):
            self.minecraftVersionBox.addItem(version)
        self.resourcePackBox.addItem(self.tr("(No resource pack)"))
        for resourcePack in sorted(install.resourcePacks):
            self.resourcePackBox.addItem(resourcePack)
        self.saveFileDir = install.getSaveFileDir()

    def getSelectedIVP(self):
        i = self.minecraftInstallBox.currentIndex()
        install = minecraftinstall.getInstall(i)
        v = self.minecraftVersionBox.currentText()
        if self.resourcePackBox.currentIndex() > 0:
            p = self.resourcePackBox.currentText()
        else:
            p = None
        return install, v, p

    def reloadList(self):
        try:
            if not os.path.isdir(self.saveFileDir):
                raise IOError(u"Could not find the Minecraft saves directory!\n\n({0} was not found or is not a directory)".format(self.saveFileDir))

            log.info("Scanning %s for worlds...", self.saveFileDir)
            potentialWorlds = os.listdir(self.saveFileDir)
            potentialWorlds = [os.path.join(self.saveFileDir, p) for p in potentialWorlds]
            worldFiles = [p for p in potentialWorlds if isLevel(AnvilWorldAdapter, p)]

            self.worldListModel = WorldListModel(worldFiles)
            self.worldListView.setModel(self.worldListModel)

            recentWorlds = RecentFilesSetting.value()
            self.recentWorldsMenu = QtGui.QMenu()

            def _triggered(f):
                def triggered():
                    self.editWorldClicked.emit(f)
                    self.accept()
                return triggered

            for filename in recentWorlds:
                displayName, lastPlayed = getWorldInfo(filename)
                action = self.recentWorldsMenu.addAction(displayName)
                action._editWorld = _triggered(filename)
                action.triggered.connect(action._editWorld)

            self.recentWorldsButton.setMenu(self.recentWorldsMenu)

        except EnvironmentError as e:
            setWidgetError(self, e)

    def openWorldClicked(self):
        QtGui.qApp.chooseOpenWorld()

    _currentFilename = None
    def worldListItemClicked(self, index):
        filename = index.data()
        if filename != self._currentFilename:
            self._currentFilename = filename
            self.showWorld(filename)

    def showWorld(self, filename):
        models = {}
        try:
            worldEditor = worldeditor.WorldEditor(filename, readonly=True)
        except (EnvironmentError, LevelFormatError) as e:
            setWidgetError(self.errorWidget, e)
            while self.stackedWidget.count():
                self.stackedWidget.removeWidget(self.stackedWidget.widget(0))

            self.worldViewBox.addWidget(self.errorWidget)
        else:
            i, v, p = self.getSelectedIVP()
            blockModels = models.get(worldEditor.blocktypes)
            resLoader = i.getResourceLoader(v, p)
            if blockModels is None:
                models[worldEditor.blocktypes] = blockModels = BlockModels(worldEditor.blocktypes, resLoader)
            textureAtlas = TextureAtlas(worldEditor, resLoader, blockModels)

            dim = worldEditor.getDimension()
            self.setWorldView(MinimapWorldView(dim, textureAtlas))
            self.chunkLoader = ChunkLoader(dim)
            self.chunkLoader.addClient(self.worldView)
            self.chunkLoader.chunkCompleted.connect(self.worldView.update)

            try:
                player = worldEditor.getPlayer()
                log.info("Centering on single-player player.")
            except PlayerNotFound:
                try:
                    center = worldEditor.worldSpawnPosition()
                    log.info("Centering on spawn position.")
                except AttributeError:
                    log.info("Centering on world center")
                    center = dim.bounds.origin + (dim.bounds.size * 0.5)
            else:
                if player.dimName == dim.dimName:
                    center = Vector(*player.Position)
                    self.worldView.centerOnPoint(center)
                else:
                    center = dim.bounds.origin + (dim.bounds.size * 0.5)

            self.worldView.centerOnPoint(center)
            log.info("Switched world view")

    def setWorldView(self, worldView):
        if self.worldView:
            self.removeWorldView()
        self.worldView = worldView
        self.stackedWidget.addWidget(worldView)

    def removeWorldView(self):
        if self.worldView:
            log.info("Removing view from WorldListWidget")
            self.worldView.textureAtlas.dispose()
            self.worldView.destroy()
            self.stackedWidget.removeWidget(self.worldView)
            self.worldView.setParent(None)
            self.worldView = None

        self.chunkLoader = None

    def hide(self):
        self.removeWorldView()
        super(WorldListWidget, self).hide()

    def close(self):
        self.removeWorldView()
        super(WorldListWidget, self).close()

    def reject(self):
        self.removeWorldView()
        super(WorldListWidget, self).reject()

    def showEvent(self, event):
        if self.worldListModel and len(self.worldListModel.worlds):
            self.worldListView.setFocus()
            self.worldListView.setCurrentIndex(self.worldListModel.createIndex(0, 0))
            self.showWorld(self.worldListModel.worlds[0][0])

    def worldListItemDoubleClicked(self, index):
        row = index.row()
        self.editWorldClicked.emit(self.worldListModel.worlds[row][0])
        self.accept()

    @profiler.function("worldListLoadTimer")
    def loadTimerFired(self):
        if not self.isVisible():
            self.loadTimer.setInterval(1000)
            return

        if self.chunkLoader:
            try:
                self.chunkLoader.next()
                self.loadTimer.setInterval(0)
            except StopIteration:
                self.loadTimer.setInterval(1000)
        else:
            self.loadTimer.setInterval(1000)

    @property
    def selectedWorldIndex(self):
        indexes = self.worldListView.selectedIndexes()
        if len(indexes):
            return indexes[0]

    editWorldClicked = QtCore.Signal(unicode)
    viewWorldClicked = QtCore.Signal(unicode)
    repairWorldClicked = QtCore.Signal(unicode)
    backupWorldClicked = QtCore.Signal(unicode)

    def editClicked(self):
        index = self.selectedWorldIndex
        if index is not None:
            self.editWorldClicked.emit(index.data(Qt.DisplayRole))
            self.accept()

    def viewClicked(self):
        index = self.selectedWorldIndex
        if index is not None:
            self.viewWorldClicked.emit(index.data(Qt.DisplayRole))
            self.accept()

    def repairClicked(self):
        index = self.selectedWorldIndex
        if index is not None:
            self.repairWorldClicked.emit(index.data(Qt.DisplayRole))
            self.accept()

    def backupClicked(self):
        index = self.selectedWorldIndex
        if index is not None:
            self.backupWorldClicked.emit(index.data(Qt.DisplayRole))
            self.accept()

    def configureClicked(self):
        installsWidget = MinecraftInstallsDialog()
        installsWidget.exec_()
        self._updateVersionsAndResourcePacks()

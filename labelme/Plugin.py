from PyQt5.QtCore import *
from PyQt5.QtGui import *
from .label_dialog import *
from .tool_bar import *
from .label_file import *
from .label_qlist_widget import *
from .escapable_qlist_widget import *
from .utils import struct
from .utils import addActions
from .utils import fmtShortcut
from .utils import newAction
from .utils import newIcon
from .color_dialog import *
import functools
import os.path as osp
import yaml
import gdal
from . import get_config
from rslabel.gui import qtMouseListener
from rslabel.gui import LabelmeEditor
from rslabel.gui import LabelmeShape  

__appname__ = 'RSLabel'


DEFAULT_LINE_COLOR = QtGui.QColor(0, 255, 0, 128)
DEFAULT_FILL_COLOR = QtGui.QColor(255, 0, 0, 128)
DEFAULT_SELECT_LINE_COLOR = QtGui.QColor(255, 255, 255)
DEFAULT_SELECT_FILL_COLOR = QtGui.QColor(0, 128, 255, 155)
DEFAULT_VERTEX_FILL_COLOR = QtGui.QColor(0, 255, 0, 255)
DEFAULT_HVERTEX_FILL_COLOR = QtGui.QColor(255, 0, 0)


class LabelmePlugin:
    def __init__(self, iface):
        gdal.AllRegister()
        self.iface=iface
        self.mainWnd = self.iface.mainWindow()
        self.canvas = iface.canvas()
        self.editor = iface.editor()
        self.menuBar = self.mainWnd.menuBar()
        config = get_config()
        self._config = config
        self.colorDialog = ColorDialog(parent=self.mainWnd)

        # Whether we need to save or not.
        self.dirty = False
        self.filename = None
        self.output_file = None
        self.output_dir = None
        self.supportedFmts = ['img','tif','hdr', 'png', 'jpg', 'ecw', 'gta', 'pix', 'hdr']
        self._noSelectionSlot = False

    def initGui(self):
        """Function initalizes GUI of the OSM Plugin.
        """
        print('* init Gui')
        self.dockWidgetVisible = False

        #mouse listener
        self.mouseListener = qtMouseListener()
        self.mouseListener.onMouseRelease = self.mouseRelease
        self.iface.addMouseListener(self.mouseListener)
   
        #Context Menus and cursor:
        self.canvasMenus = (QtWidgets.QMenu(), QtWidgets.QMenu())   

        # Main widgets and related state.
        self.labelDialog = LabelDialog(
            parent=self.mainWnd,
            labels=self._config['labels'],
            sort_labels=self._config['sort_labels'],
            show_text_field=self._config['show_label_text_field'],
            completion=self._config['label_completion'],
            fit_to_content=self._config['fit_to_content'],
        )
        self.createDockWidgets()        
        self.createActionsAndMenus()
        #add tool bar to main window
        self.tools = self.toolbar('Tools')
        self.populateModeActions()
        self.setSignals()
     

        # Application state.
        self.image = QtGui.QImage()
        self.imagePath = None
        self.recentFiles = []
        self.maxRecent = 7
        self.lineColor = DEFAULT_LINE_COLOR 
        self.fillColor = DEFAULT_FILL_COLOR
        self.otherData = None

        #config and settings       
        # XXX: Could be completely declarative.
        # Restore application settings.
        self.settings = QtCore.QSettings('labelme', 'labelme')
        # FIXME: QSettings.value can return None on PyQt4
        self.recentFiles = self.settings.value('recentFiles', []) or []
       
        # Populate the File menu dynamically.
        self.updateFileMenu()
        # Since loading the file may take some time,
        # make sure it runs in the background.
        if self.filename is not None:
            self.queueEvent(functools.partial(self.loadFile, self.filename))
        
        self.statusBar().showMessage('%s started.' % __appname__)
        self.statusBar().show()
      
    def fileSearchChanged(self):
        self.importDirImages(
            self.lastOpenDir,
            pattern=self.fileSearch.text(),
            load=False,
        )

    # Message Dialogs. #
    def hasLabels(self):
        if not self.labelList.itemsToShapes:
            self.errorMessage(
                'No objects labeled',
                'You must label at least one object to save the file.')
            return False
        return True

    def mayContinue(self):
        if not self.dirty:
            return True
        mb = QtWidgets.QMessageBox
        msg = '在关闭之前将标记保存到"{}" ?'.format(self.filename)
        answer = mb.question(self.mainWnd,
                             '保存标记?',
                             msg,
                             mb.Save | mb.Discard | mb.Cancel,
                             mb.Save)
        if answer == mb.Discard:
            return True
        elif answer == mb.Save:
            self.saveFile()
            return True
        else:  # answer == mb.Cancel
            return False

    def fileSelectionChanged(self):
        items = self.fileListWidget.selectedItems()
        if not items:
            return
        item = items[0]

        if not self.mayContinue():
            return

        currIndex = self.imageList.index(str(item.text()))
        if currIndex < len(self.imageList):
            filename = self.imageList[currIndex]
            if filename:
                self.iface.reset() #
                self.loadFile(filename)


    def setDirty(self):
        if self._config['auto_save'] or self.actions.saveAuto.isChecked():
            label_file = osp.splitext(self.imagePath)[0] + '.json'
            if self.output_dir:
                label_file = osp.join(self.output_dir, label_file)
            self.saveLabels(label_file)
            return
        self.dirty = True
        self.actions.save.setEnabled(True)
        self.actions.undo.setEnabled(self.editor.isShapeRestorable())
        title = __appname__
        if self.filename is not None:
            title = '{} - {}*'.format(title, self.filename)
        self.mainWnd.setWindowTitle(title)
        print('* set dirty')
        

    # Callback functions:
    def newShape(self):
        """Pop-up and give focus to the label editor.

        position MUST be in global coordinates.
        """
        items = self.uniqLabelList.selectedItems()
        text = None
        if items:
            text = items[0].text()
        if self._config['display_label_popup'] or not text:
            text = self.labelDialog.popUp(text)
        if text is not None and not self.validateLabel(text):
            self.errorMessage('无效的标签',
                              "Invalid label '{}' with validation type '{}'"
                              .format(text, self._config['validate_label']))
            text = None
        if text is None:
            self.editor.undoLastLine()
            #self.editor.shapesBackups.pop()
        else:
            self.addLabel(self.editor.setLastLabel(text))
            self.editor.commit()
            self.actions.editMode.setEnabled(True)
            self.actions.undoLastPoint.setEnabled(False)
            self.actions.undo.setEnabled(True)
            self.setDirty()

    def  editLabel(self, item=None):
        print('*editLabel')
        if not self.editor.isEditing():
            print('*editLabel, not editing. return')
            return
        item = item if item else self.currentItem()
        text = self.labelDialog.popUp(item.text())
        if text is None:
            return
        if not self.validateLabel(text):
            self.errorMessage('Invalid label',
                              "Invalid label '{}' with validation type '{}'"
                              .format(text, self._config['validate_label']))
            return
        item.setText(text)
        self.setDirty()
        if not self.uniqLabelList.findItems(text, Qt.MatchExactly):
            self.uniqLabelList.addItem(text)
            self.uniqLabelList.sortItems()


    # React to canvas signals.
    def shapeSelectionChanged(self, selected=False):
        print('* shapeSelectionChanged slot triggered')
        if self._noSelectionSlot:
            self._noSelectionSlot = False
        else:
            shape = self.editor.selectedShape()
            if shape:
                item = self.labelList.get_item_from_shape(shape)
                item.setSelected(True)
            else:
                self.labelList.clearSelection()
        self.actions.delete.setEnabled(selected)
        self.actions.copy.setEnabled(selected)
        self.actions.edit.setEnabled(selected)
        self.actions.shapeLineColor.setEnabled(selected)
        self.actions.shapeFillColor.setEnabled(selected)

    def addLabel(self, shape):
        item = QtWidgets.QListWidgetItem(shape.getLabel())
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)
        self.labelList.itemsToShapes.append((item, shape))
        self.labelList.addItem(item)
        if not self.uniqLabelList.findItems(shape.getLabel(), Qt.MatchExactly):
            self.uniqLabelList.addItem(shape.getLabel())
            self.uniqLabelList.sortItems()
        self.labelDialog.addLabelHistory(item.text())
        for action in self.actions.onShapesPresent:
            action.setEnabled(True)

    def noShapes(self):
        return not self.labelList.itemsToShapes

    def remLabel(self, shape):
        item = self.labelList.get_item_from_shape(shape)
        self.labelList.takeItem(self.labelList.row(item))

    def loadShapes(self, shapes):
        for shape in shapes:
            self.addLabel(shape)
        self.editor.loadShapes(shapes)

    def loadLabels(self, shapes):
        print('* Plugin. load labels')
        s = []
        for label, points, line_color, fill_color, shape_type in shapes:
            print('*',type(label))
            print('*',type(shape_type))
            print('* label',label)
            print('* shape type', shape_type)
            print('* line color', line_color)
            print('* fill color', fill_color)
            shape = LabelmeShape(label, shape_type)
            for x, y in points:
                print('*({},{})'.format(x,y))
                shape.addPoint(QtCore.QPoint(x, y))
            shape.close()
            s.append(shape)
            if line_color:
                shape.line_color = QtGui.QColor(*line_color)
            if fill_color:
                shape.fill_color = QtGui.QColor(*fill_color)
        self.loadShapes(s)

    def loadFlags(self, flags):
        self.flag_widget.clear()
        for key, flag in flags.items():
            item = QtWidgets.QListWidgetItem(key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if flag else Qt.Unchecked)
            self.flag_widget.addItem(item)

    # called by saveFile
    def saveLabels(self, filename):
        lf = LabelFile()
        def format_shape(s):
            print('*',len(s.thePoints))
            return dict(
                label= s.getLabel(),
                line_color=s.line_color.getRgb()
                if s.line_color != self.lineColor else None,
                fill_color=s.fill_color.getRgb()
                if s.fill_color != self.fillColor else None,
                points=[(p.x(), p.y()) for p in s.thePoints],
                shape_type=s.getType(),
            )

        shapes = [format_shape(shape) for shape in self.labelList.shapes]
        flags = {}
        for i in range(self.flag_widget.count()):
            item = self.flag_widget.item(i)
            key = item.text()
            flag = item.checkState() == Qt.Checked
            flags[key] = flag
        try:
            imagePath = osp.relpath(
                self.imagePath, osp.dirname(filename))
            imageData = self.imageData if self._config['store_data'] else None
            if osp.dirname(filename) and not osp.exists(osp.dirname(filename)):
                os.makedirs(osp.dirname(filename))
            lf.save(
                filename=filename,
                shapes=shapes,
                imagePath=imagePath,
                imageData=imageData,
                imageHeight=self.image.height(),
                imageWidth=self.image.width(),
                lineColor=self.lineColor.getRgb(),
                fillColor=self.fillColor.getRgb(),
                otherData=self.otherData,
                flags=flags,
            )
            self.labelFile = lf
            items = self.fileListWidget.findItems(
                self.imagePath, Qt.MatchExactly
            )
            if len(items) > 0:
                if len(items) != 1:
                    raise RuntimeError('There are duplicate files.')
                items[0].setCheckState(Qt.Checked)
            # disable allows next and previous image to proceed
            # self.filename = filename
            return True
        except LabelFileError as e:
            self.errorMessage('Error saving label data', '<b>%s</b>' % e)
            return False


    def importDirImages(self, dirpath, pattern=None, load=True):
        self.actions.openNextImg.setEnabled(True)
        self.actions.openPrevImg.setEnabled(True)

        if not self.mayContinue() or not dirpath:
            return

        self.lastOpenDir = dirpath
        self.filename = None
        self.fileListWidget.clear()
        for filename in self.scanAllImages(dirpath):
            if pattern and pattern not in filename:
                continue
            label_file = osp.splitext(filename)[0] + '.json'
            if self.output_dir:
                label_file = osp.join(self.output_dir, label_file)
            item = QtWidgets.QListWidgetItem(filename)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if QtCore.QFile.exists(label_file) and \
                    LabelFile.isLabelFile(label_file):
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)
            self.fileListWidget.addItem(item)
        self.openNextImg(load=load)

    def undoShapeEdit(self):
        self.canvas.restoreShape()
        self.labelList.clear()
        self.loadShapes(self.canvas.shapes)
        self.actions.undo.setEnabled(self.canvas.isShapeRestorable)

    def togglePolygons(self, value):
        for item, shape in self.labelList.itemsToShapes:
            item.setCheckState(Qt.Checked if value else Qt.Unchecked)

    def openDirDialog(self, _value=False, dirpath=None):
        if not self.mayContinue():
            return

        defaultOpenDirPath = dirpath if dirpath else '.'
        if self.lastOpenDir and osp.exists(self.lastOpenDir):
            defaultOpenDirPath = self.lastOpenDir
        else:
            defaultOpenDirPath = osp.dirname(self.filename) \
                if self.filename else '.'

        targetDirPath = str(QtWidgets.QFileDialog.getExistingDirectory(
            self.mainWnd, '%s - Open Directory' % __appname__, defaultOpenDirPath,
            QtWidgets.QFileDialog.ShowDirsOnly |
            QtWidgets.QFileDialog.DontResolveSymlinks))
        self.importDirImages(targetDirPath)

    def openFile(self, _value=False):
        if not self.mayContinue():
            return
        mainwnd = self.iface.mainWindow()
        path = osp.dirname(str(self.filename)) if self.filename else '.'
        #formats = ['*.{}'.format(fmt.data().decode())
                   #for fmt in QtGui.QImageReader.supportedImageFormats()]
        formats = ['*.tif', '*.pix', '*.pci', '*.hdr', '*.img']
        filters = "Image & Label files (%s)" % ' '.join(
            formats + ['*%s' % LabelFile.suffix])
        filename = QtWidgets.QFileDialog.getOpenFileName(
            mainwnd, '%s - Choose Image or Label file' % __appname__,
            path, filters)
        filename, _ = filename
        filename = str(filename)
        if filename:
            self.loadFile(filename)

    def openPrevImg(self, _value=False):
        keep_prev = self._config['keep_prev']
        if QtGui.QGuiApplication.keyboardModifiers() == \
                (QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier):
            self._config['keep_prev'] = True
        if not self.mayContinue():
            return
        if len(self.imageList) <= 0:
            return
        if self.filename is None:
            return
        currIndex = self.imageList.index(self.filename)
        if currIndex - 1 >= 0:
            filename = self.imageList[currIndex - 1]
            if filename:
                self.loadFile(filename)
        self._config['keep_prev'] = keep_prev


    def openNextImg(self, _value=False, load=True):
        keep_prev = self._config['keep_prev']
        if QtGui.QGuiApplication.keyboardModifiers() == \
                (QtCore.Qt.ControlModifier | QtCore.Qt.ShiftModifier):
            self._config['keep_prev'] = True
        if not self.mayContinue():
            return
        if len(self.imageList) <= 0:
            return
        filename = None
        if self.filename is None:
            filename = self.imageList[0]
        else:
            currIndex = self.imageList.index(self.filename)
            if currIndex + 1 < len(self.imageList):
                filename = self.imageList[currIndex + 1]
            else:
                filename = self.imageList[-1]
        self.filename = filename
        if self.filename and load:
            self.loadFile(self.filename)
        self._config['keep_prev'] = keep_prev

    def saveFile(self, _value=False):
        if self._config['flags'] or self.hasLabels():
            print('*save File')
            if self.labelFile:
                # DL20180323 - overwrite when in directory
                print('* has label file')
                self._saveFile(self.labelFile.filename)
            elif self.output_file:
                print('has output file')
                self._saveFile(self.output_file)
                self.close()
            else:
                print('*call _saveFile')
                self._saveFile(self.saveFileDialog())

    #add toolbar the main window
    def toolbar(self, title, actions=None):
        toolbar = ToolBar(title)
        toolbar.setObjectName('%sToolBar' % title)
        # toolbar.setOrientation(Qt.Vertical)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        if actions:
            addActions(toolbar, actions)
        self.iface.addToolbar(toolbar)
        return toolbar

    #add menu to main window
    def menu(self, title, actions=None):
        menu = self.menuBar.addMenu(title)
        if actions:
            addActions(menu, actions)
        return menu

    def populateModeActions(self):
        tool, menu = self.actions.tool, self.actions.menu
        self.tools.clear()
        addActions(self.tools, tool)
        # self.canvasMenus[0].clear() *why clear here? 
        # addActions(self.iface.canvas(), menu)
        self.menus.edit.clear()
        actions = (
            self.actions.createMode,
            self.actions.createRectangleMode,
            self.actions.createCircleMode,
            self.actions.createLineMode,
            self.actions.createPointMode,
            self.actions.createLineStripMode,
            self.actions.editMode,
        )
        addActions(self.menus.edit, actions + self.actions.editMenu) 

    def saveFileAs(self, _value=False):
        assert not self.image.isNull(), "cannot save empty image"
        if self.hasLabels():
            self._saveFile(self.saveFileDialog())

    def currentPath(self):
        return osp.dirname(str(self.filename)) if self.filename else '.'

    def saveFileDialog(self):
        caption = '%s - Choose File' % __appname__
        filters = 'Label files (*%s)' % LabelFile.suffix
        if self.output_dir:
            dlg = QtWidgets.QFileDialog(
                self, caption, self.output_dir, filters
            )
        else:
            dlg = QtWidgets.QFileDialog(
                self.mainWnd, caption, self.currentPath(), filters
            )
        dlg.setDefaultSuffix(LabelFile.suffix[1:])
        dlg.setAcceptMode(QtWidgets.QFileDialog.AcceptSave)
        dlg.setOption(QtWidgets.QFileDialog.DontConfirmOverwrite, False)
        dlg.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, False)
        basename = osp.splitext(self.filename)[0]
        if self.output_dir:
            default_labelfile_name = osp.join(
                self.output_dir, basename + LabelFile.suffix
            )
        else:
            default_labelfile_name = osp.join(
                self.currentPath(), basename + LabelFile.suffix
            )
        filename = dlg.getSaveFileName(
            self.mainWnd, 'Choose File', default_labelfile_name,
            'Label files (*%s)' % LabelFile.suffix)
        filename, _ = filename
        filename = str(filename)
        return filename
   
 
    def changeOutputDirDialog(self, _value=False):
        default_output_dir = self.output_dir
        if default_output_dir is None and self.filename:
            default_output_dir = osp.dirname(self.filename)
        if default_output_dir is None:
            default_output_dir = self.currentPath()

        output_dir = QtWidgets.QFileDialog.getExistingDirectory(
            self, '%s - Save/Load Annotations in Directory' % __appname__,
            default_output_dir,
            QtWidgets.QFileDialog.ShowDirsOnly |
            QtWidgets.QFileDialog.DontResolveSymlinks,
        )
        output_dir = str(output_dir)

        if not output_dir:
            return

        self.output_dir = output_dir

        self.statusBar().showMessage(
            '%s . Annotations will be saved/loaded in %s' %
            ('Change Annotations Dir', self.output_dir))
        self.statusBar().show()

        current_filename = self.filename
        self.importDirImages(self.lastOpenDir, load=False)

        if current_filename in self.imageList:
            # retain currently selected file
            self.fileListWidget.setCurrentRow(
                self.imageList.index(current_filename))
            self.fileListWidget.repaint()
 
    def _saveFile(self, filename):
        if filename and self.saveLabels(filename):
            self.addRecentFile(filename)
            self.setClean()

    def closeFile(self, _value=False):
        if not self.mayContinue():
            return
        self.resetState()
        self.setClean()
        self.toggleActions(False)
        self.canvas.setEnabled(False)
        self.actions.saveAs.setEnabled(False)

     
    def chooseColor1(self):
        color = self.colorDialog.getColor(
            self.lineColor, 'Choose line color', default=DEFAULT_LINE_COLOR)
        if color:
            self.lineColor = color
            # Change the color for all shape lines:
            #Shape.line_color = self.lineColor
            self.editor.setLineColor(self.lineColor)
            self.canvas.update()
            self.setDirty()

    def chooseColor2(self):
        color = self.colorDialog.getColor(
            self.fillColor, 'Choose fill color', default=DEFAULT_FILL_COLOR)
        if color:
            self.fillColor = color
            #Shape.fill_color = self.fillColor
            self.editor.setFillColor(self.fillColor)
            self.canvas.update()
            self.setDirty()

    
    def toggleKeepPrevMode(self):
        self._config['keep_prev'] = not self._config['keep_prev']

    def deleteSelectedShape(self):
        yes, no = QtWidgets.QMessageBox.Yes, QtWidgets.QMessageBox.No
        #msg = 'You are about to permanently delete this polygon, ' \
        #      'proceed anyway?'
        msg = '您正准备永久删除这个多边形, ' \
              '无论如何继续?'
        if yes == QtWidgets.QMessageBox.warning(self.mainWnd, '注意', msg,
                                                yes | no):
            self.remLabel(self.editor.deleteSelected())
            self.setDirty()
            if self.noShapes():
                for action in self.actions.onShapesPresent:
                    action.setEnabled(False)

    def chshapeLineColor(self):
        color = self.colorDialog.getColor(
            self.lineColor, 'Choose line color', default=DEFAULT_LINE_COLOR)
        if color:
            self.canvas.selectedShape.line_color = color
            self.canvas.update()
            self.setDirty()

    def chshapeFillColor(self):
        color = self.colorDialog.getColor(
            self.fillColor, 'Choose fill color', default=DEFAULT_FILL_COLOR)
        if color:
            self.canvas.selectedShape.fill_color = color
            self.canvas.update()
            self.setDirty()

    def copyShape(self):
        self.canvas.endMove(copy=True)
        self.addLabel(self.canvas.selectedShape)
        self.setDirty()

    def moveShape(self):
        self.canvas.endMove(copy=False)
        self.setDirty()    


    def copySelectedShape(self):
        print('*()copy selected shape')
        self.addLabel(self.editor.copySelectedShape())
        # fix copy and delete
        self.shapeSelectionChanged(True)

    def resetState(self):
        self.labelList.clear()
        self.filename = None
        self.imagePath = None
        self.imageData = None
        self.labelFile = None
        self.otherData = None
        # self.canvas.resetState() *

    @property
    def imageList(self):
        lst = []
        for i in range(self.fileListWidget.count()):
            item = self.fileListWidget.item(i)
            lst.append(item.text())
        return lst

    def loadFile(self, filename=None):
        """Load the specified file, or the last opened file if None."""
        # changing fileListWidget loads file
        print('*\n\n\n-------------------------------------load a new file --------------------------------------------')
        if (filename in self.imageList and
                self.fileListWidget.currentRow() !=
                self.imageList.index(filename)):
            self.fileListWidget.setCurrentRow(self.imageList.index(filename))
            self.fileListWidget.repaint()
            return
        self.resetState()
        # self.canvas.setEnabled(False) *
        if filename is None:
            filename = self.settings.value('filename', '')
        filename = str(filename)
        if not QtCore.QFile.exists(filename):
            self.errorMessage(
                'Error opening file', 'No such file: <b>%s</b>' % filename)
            return False
        # assumes same name, but json extension
        self.status("Loading %s..." % osp.basename(str(filename)))
        label_file = osp.splitext(filename)[0] + '.json'
        if self.output_dir:
            label_file = osp.join(self.output_dir, label_file)
        #if find the label file for the image
        if QtCore.QFile.exists(label_file) and \
                LabelFile.isLabelFile(label_file):
            try:
                self.labelFile = LabelFile(label_file)
            except LabelFileError as e:
                self.errorMessage(
                    'Error opening file',
                    "<p><b>%s</b></p>"
                    "<p>Make sure <i>%s</i> is a valid label file."
                    % (e, label_file))
                self.status("Error reading %s" % label_file)
                return False
            self.imagePath = osp.join(
                osp.dirname(label_file),
                self.labelFile.imagePath,
            )
            self.lineColor = QtGui.QColor(*self.labelFile.lineColor)
            self.fillColor = QtGui.QColor(*self.labelFile.fillColor)
            self.otherData = self.labelFile.otherData
        else:
            # Load image:
            # read data first and store for saving into label file.
            # self.imageData = read(filename, None) *
            imageHandle = read(filename) #*
            if imageHandle is not None:
                # the filename is image not JSON
                self.imagePath = filename
                '''
                if QtGui.QImage.fromData(self.imageData).isNull():
                    self.imageData = self.convertImageDataToPng(self.imageData)
                '''
                del imageHandle
                self.labelFile = None
            else:
                formats = ['*.{}'.format(fmt.data().decode())
                        for fmt in QtGui.QImageReader.supportedImageFormats()]
                self.errorMessage(
                    'Error opening file',
                    '<p>Make sure <i>{0}</i> is a valid image file.<br/>'
                    'Supported image formats: {1}</p>'
                    .format(filename, ','.join(formats)))
                self.status("Error reading %s" % filename)
                return False
            
        self.filename = filename
        if self._config['keep_prev']:
            prev_shapes = self.canvas.shapes
        
        # to display the image
        # self.canvas.loadPixmap(filename) 
        self.editor.clearShapes()
        if self._config['flags']:
            self.loadFlags({k: False for k in self._config['flags']})
        if self._config['keep_prev']:
            self.loadShapes(prev_shapes)
            print('* load shapes prev ~!~')
        if self.labelFile:
            self.loadLabels(self.labelFile.shapes) #his shapes is not labelmeShape
            if self.labelFile.flags is not None:
                self.loadFlags(self.labelFile.flags)
        else:
            print('*the labelFile is None')

        self.setClean()
        self.paintCanvas()
        self.addRecentFile(self.filename)
        self.toggleActions(True)
        self.status("Loaded %s" % osp.basename(str(filename)))
        return True 

    def setClean(self):
        self.dirty = False
        self.actions.save.setEnabled(False)
        self.actions.createMode.setEnabled(True)
        self.actions.createRectangleMode.setEnabled(True)
        self.actions.createCircleMode.setEnabled(True)
        self.actions.createLineMode.setEnabled(True)
        self.actions.createPointMode.setEnabled(True)
        self.actions.createLineStripMode.setEnabled(True)
        title = __appname__
        if self.filename is not None:
            title = '{} - {}'.format(title, self.filename)
        self.mainWnd.setWindowTitle(title)

    def unload(self):
        """Function unloads the OSM Plugin.
        """
        self.dockWidget.close()

    def setEditMode(self):
        print('**set edit mode')
        self.toggleDrawMode(True)
 
    def showHideDockWidget(self):
        """Function shows/hides main dockable widget of the plugin ("OSM Feature" widget)
        """
        if self.dockWidget.isVisible():
            self.dockWidget.hide()
        else:
            self.dockWidget.show()

    # Callbacks
    def undoShapeEdit(self):
        '''
        self.editor.restoreShape()
        self.labelList.clear()
        self.loadShapes(self.editor.theShapes)
        self.actions.undo.setEnabled(self.editor.isShapeRestorable())
        '''

    def tutorial(self):
        url = 'https://github.com/wkentaro/labelme/tree/master/examples/tutorial'  # NOQA
        webbrowser.open(url)

    def toggleAddPointEnabled(self, enabled):
        self.actions.addPoint.setEnabled(enabled)

    def toggleDrawingSensitive(self, drawing=True):
        """Toggle drawing sensitive.

        In the middle of drawing, toggling between modes should be disabled.
        """
        print('* toggleDrawingSensitive')
        self.actions.editMode.setEnabled(not drawing)
        self.actions.undoLastPoint.setEnabled(drawing)
        self.actions.undo.setEnabled(not drawing)
        self.actions.delete.setEnabled(not drawing)


    def toggleDrawMode(self, edit=True, createMode='polygon'):
        self.editor.setEditing(edit)
        self.editor.setCreateMode(createMode)   # set canvas 's creata mode.  *
        if edit:           
            self.actions.createMode.setEnabled(True)
            self.actions.createRectangleMode.setEnabled(True)
            self.actions.createCircleMode.setEnabled(True)
            self.actions.createLineMode.setEnabled(True)
            self.actions.createPointMode.setEnabled(True)
            self.actions.createLineStripMode.setEnabled(True)
        else:
            if createMode == 'polygon':
                self.actions.createMode.setEnabled(False)
                self.actions.createRectangleMode.setEnabled(True)
                self.actions.createCircleMode.setEnabled(True)
                self.actions.createLineMode.setEnabled(True)
                self.actions.createPointMode.setEnabled(True)
                self.actions.createLineStripMode.setEnabled(True)
            elif createMode == 'rectangle':
                self.actions.createMode.setEnabled(True)
                self.actions.createRectangleMode.setEnabled(False)
                self.actions.createCircleMode.setEnabled(True)
                self.actions.createLineMode.setEnabled(True)
                self.actions.createPointMode.setEnabled(True)
                self.actions.createLineStripMode.setEnabled(True)
            elif createMode == 'line':
                self.actions.createMode.setEnabled(True)
                self.actions.createRectangleMode.setEnabled(True)
                self.actions.createCircleMode.setEnabled(True)
                self.actions.createLineMode.setEnabled(False)
                self.actions.createPointMode.setEnabled(True)
                self.actions.createLineStripMode.setEnabled(True)
            elif createMode == 'point':
                self.actions.createMode.setEnabled(True)
                self.actions.createRectangleMode.setEnabled(True)
                self.actions.createCircleMode.setEnabled(True)
                self.actions.createLineMode.setEnabled(True)
                self.actions.createPointMode.setEnabled(False)
                self.actions.createLineStripMode.setEnabled(True)
            elif createMode == "circle":
                self.actions.createMode.setEnabled(True)
                self.actions.createRectangleMode.setEnabled(True)
                self.actions.createCircleMode.setEnabled(False)
                self.actions.createLineMode.setEnabled(True)
                self.actions.createPointMode.setEnabled(True)
                self.actions.createLineStripMode.setEnabled(True)
            elif createMode == "linestrip":
                self.actions.createMode.setEnabled(True)
                self.actions.createRectangleMode.setEnabled(True)
                self.actions.createCircleMode.setEnabled(True)
                self.actions.createLineMode.setEnabled(True)
                self.actions.createPointMode.setEnabled(True)
                self.actions.createLineStripMode.setEnabled(False)
            else:
                raise ValueError('Unsupported createMode: %s' % createMode)
        self.actions.editMode.setEnabled(not edit)


    def createDockWidgets(self):
        self.labelList = LabelQListWidget()
        self.lastOpenDir = None
        self.labelList.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.labelList.setParent(self.mainWnd)
        self.shape_dock = QtWidgets.QDockWidget('Polygon Labels', self.mainWnd)
        self.shape_dock.setObjectName('Labels')
        self.shape_dock.setWidget(self.labelList)
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.shape_dock) #


        self.uniqLabelList = EscapableQListWidget()
        self.uniqLabelList.setToolTip(
            "Select label to start annotating for it. "
            "Press 'Esc' to deselect.")
        if self._config['labels']:
            self.uniqLabelList.addItems(self._config['labels'])
            self.uniqLabelList.sortItems()
        self.label_dock = QtWidgets.QDockWidget(u'Label List', self.mainWnd)
        self.label_dock.setObjectName(u'Label List')
        self.label_dock.setWidget(self.uniqLabelList)
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.label_dock) #

        self.fileSearch = QtWidgets.QLineEdit()
        self.fileSearch.setPlaceholderText('Search Filename')
        self.fileSearch.textChanged.connect(self.fileSearchChanged)
        self.fileListWidget = QtWidgets.QListWidget()
        self.fileListWidget.itemSelectionChanged.connect(
            self.fileSelectionChanged
        )
        fileListLayout = QtWidgets.QVBoxLayout()
        fileListLayout.setContentsMargins(0, 0, 0, 0)
        fileListLayout.setSpacing(0)
        fileListLayout.addWidget(self.fileSearch)
        fileListLayout.addWidget(self.fileListWidget)
        self.file_dock = QtWidgets.QDockWidget(u'File List', self.mainWnd)
        self.file_dock.setObjectName(u'Files')
        fileListWidget = QtWidgets.QWidget()
        fileListWidget.setLayout(fileListLayout)
        self.file_dock.setWidget(fileListWidget)
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.file_dock) #*

        self.flag_dock = self.flag_widget = None
        self.flag_dock = QtWidgets.QDockWidget('Flags', self.mainWnd)
        self.flag_dock.setObjectName('Flags')
        self.flag_widget = QtWidgets.QListWidget()
        if self._config['flags']:
            self.loadFlags({k: False for k in config['flags']})
        self.flag_dock.setWidget(self.flag_widget)
        self.flag_widget.itemChanged.connect(self.setDirty)
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.flag_dock)
        #signals and slots
        self.labelList.itemActivated.connect(self.labelSelectionChanged)
        self.labelList.itemSelectionChanged.connect(self.labelSelectionChanged)
        self.labelList.itemDoubleClicked.connect(self.editLabel)
        # Connect to itemChanged to detect checkbox changes.
        self.labelList.itemChanged.connect(self.labelItemChanged)
        self.labelList.setDragDropMode(
            QtWidgets.QAbstractItemView.InternalMove)

    # Actions and menus
    def createActionsAndMenus(self):
        action = functools.partial(newAction, self.mainWnd) #*
        shortcuts = self._config['shortcuts']
        quit = action('&退出', self.mainWnd.close, shortcuts['quit'], 'quit',
                      'Quit application')
        open_ = action('&打开', self.openFile, shortcuts['open'], 'open',
                       'Open image or label file')
        #opendir = action('&Open Dir', self.openDirDialog,
        opendir = action('&打开文件夹', self.openDirDialog,
                         shortcuts['open_dir'], 'open', u'Open Dir')
        openNextImg = action(
            '&下一幅图像',
            self.openNextImg,
            shortcuts['open_next'],
            'next',
            u'Open next (hold Ctl+Shift to copy labels)',
            enabled=False,
        )
        openPrevImg = action(
            '&前一幅图像',
            self.openPrevImg,
            shortcuts['open_prev'],
            'prev',
            u'Open prev (hold Ctl+Shift to copy labels)',
            enabled=False,
        )
        save = action('&保存', self.saveFile, shortcuts['save'], 'save',
                      'Save labels to file', enabled=False)
        saveAs = action('&另存为', self.saveFileAs, shortcuts['save_as'],
                        'save-as', 'Save labels to a different file',
                        enabled=False)
        #exportAs = action('&导出为', self.exportAs, shortcuts['export_as'],
        #                'export-as', 'export labels to a different dataset format',
        #                enabled=False)
        changeOutputDir = action(
            '&改变输出目录',
            slot=self.changeOutputDirDialog,
            shortcut=shortcuts['save_to'],
            icon='open',
            tip=u'Change where annotations are loaded/saved'
        )

        saveAuto = action(
            text='自动 &保存',
            slot=lambda x: self.actions.saveAuto.setChecked(x),
            icon='save',
            tip='自动保存',
            checkable=True,
            enabled=True,
        )
        saveAuto.setChecked(self._config['auto_save'])

        close = action('&关闭', self.closeFile, shortcuts['close'], 'close',
                       'Close current file')
        color1 = action('多边形 &线条 颜色', self.chooseColor1,
                        shortcuts['edit_line_color'], 'color_line',
                        'Choose polygon line color')
        color2 = action('多边形 &填充颜色', self.chooseColor2,
                        shortcuts['edit_fill_color'], 'color',
                        'Choose polygon fill color')

        toggle_keep_prev_mode = action(
            'Keep Previous Annotation',
            self.toggleKeepPrevMode,
            shortcuts['toggle_keep_prev_mode'], None,
            'Toggle "keep pevious annotation" mode',
            checkable=True)
        toggle_keep_prev_mode.setChecked(self._config['keep_prev'])

        createMode = action(
            '创建多边形',
            lambda: self.toggleDrawMode(False, createMode='polygon'),
            shortcuts['create_polygon'],
            'objects',
            'Start drawing polygons',
            enabled=False,
        )
        createRectangleMode = action(
            '创建矩形',
            lambda: self.toggleDrawMode(False, createMode='rectangle'),
            shortcuts['create_rectangle'],
            'objects',
            'Start drawing rectangles',
            enabled=False,
        )
        createCircleMode = action(
            '创建圆形 ',
            lambda: self.toggleDrawMode(False, createMode='circle'),
            shortcuts['create_circle'],
            'objects',
            'Start drawing circles',
            enabled=False,
        )
        createLineMode = action(
            '创建线段',
            lambda: self.toggleDrawMode(False, createMode='line'),
            shortcuts['create_line'],
            'objects',
            'Start drawing lines',
            enabled=False,
        )
        createPointMode = action(
            '创建点',
            lambda: self.toggleDrawMode(False, createMode='point'),
            shortcuts['create_point'],
            'objects',
            'Start drawing points',
            enabled=False,
        )
        createLineStripMode = action(
            '创建线条',
            lambda: self.toggleDrawMode(False, createMode='linestrip'),
            shortcuts['create_linestrip'],
            'objects',
            'Start drawing linestrip. Ctrl+LeftClick ends creation.',
            enabled=False,
        )
        editMode = action('编辑多边形', self.setEditMode,
                          shortcuts['edit_polygon'], 'edit',
                          'Move and edit polygons', enabled=False)

        delete = action('删除多边形', self.deleteSelectedShape,
                        shortcuts['delete_polygon'], 'cancel',
                        'Delete', enabled=False)
        copy = action('复制多边形', self.copySelectedShape,
                      shortcuts['duplicate_polygon'], 'copy',
                      'Create a duplicate of the selected polygon',
                      enabled=False)
        undoLastPoint = action('撤销最后的点', self.editor.undoLastPoint,
                               shortcuts['undo_last_point'], 'undo',
                               'Undo last drawn point', enabled=False)
        addPoint = action('在边上添加点', self.editor.addPointToEdge,
                          None, 'edit', 'Add point to the nearest edge',
                          enabled=False)
        undo = action('撤销', self.undoShapeEdit, shortcuts['undo'], 'undo',
                      'Undo last add and edit of shape', enabled=False)

        hideAll = action('&隐藏\n多边形',
                         functools.partial(self.togglePolygons, False),
                         icon='eye', tip='Hide all polygons', enabled=False)
        showAll = action('&显示\n多边形',
                         functools.partial(self.togglePolygons, True),
                         icon='eye', tip='Show all polygons', enabled=False)

        help = action('&教程', self.tutorial, icon='help',
                      tip='Show tutorial page')

        ''' *
        zoom = QtWidgets.QWidgetAction(self)
        zoom.setDefaultWidget(self.zoomWidget)
        self.zoomWidget.setWhatsThis(
            "Zoom in or out of the image. Also accessible with"
            " %s and %s from the canvas." %
            (fmtShortcut('%s,%s' % (shortcuts['zoom_in'],
                                    shortcuts['zoom_out'])),
             fmtShortcut("Ctrl+Wheel")))
        self.zoomWidget.setEnabled(False)

        zoomIn = action('Zoom &In', functools.partial(self.addZoom, 10),
                        shortcuts['zoom_in'], 'zoom-in',
                        'Increase zoom level', enabled=False)
        zoomOut = action('&Zoom Out', functools.partial(self.addZoom, -10),
                         shortcuts['zoom_out'], 'zoom-out',
                         'Decrease zoom level', enabled=False)
        zoomOrg = action('&Original size',
                         functools.partial(self.setZoom, 100),
                         shortcuts['zoom_to_original'], 'zoom',
                         'Zoom to original size', enabled=False)
        fitWindow = action('&Fit Window', self.setFitWindow,
                           shortcuts['fit_window'], 'fit-window',
                           'Zoom follows window size', checkable=True,
                           enabled=False)
        fitWidth = action('Fit &Width', self.setFitWidth,
                          shortcuts['fit_width'], 'fit-width',
                          'Zoom follows window width',
                          checkable=True, enabled=False)
        # Group zoom controls into a list for easier toggling.
        zoomActions = (self.zoomWidget, zoomIn, zoomOut, zoomOrg,
                       fitWindow, fitWidth)
        self.zoomMode = self.FIT_WINDOW
        fitWindow.setChecked(Qt.Checked)
        self.scalers = {
            self.FIT_WINDOW: self.scaleFitWindow,
            self.FIT_WIDTH: self.scaleFitWidth,
            # Set to one to scale to 100% when loading files.
            self.MANUAL_ZOOM: lambda: 1,
        }
        '''

        edit = action('&编辑标记', self.editLabel, shortcuts['edit_label'],
                      'edit', 'Modify the label of the selected polygon',
                      enabled=False)

        shapeLineColor = action(
            '图形&线条颜色', self.chshapeLineColor, icon='color-line',
            tip='Change the line color for this specific shape', enabled=False)
        shapeFillColor = action(
            '图形&填充颜色', self.chshapeFillColor, icon='color',
            tip='Change the fill color for this specific shape', enabled=False)
        fill_drawing = action(
            '填充正在绘制的多边形',
            lambda x: self.canvas.setFillDrawing(x),
            None,
            'color',
            #'Fill polygon while drawing',
            '在绘制时填充多边形',
            checkable=True,
            enabled=True,
        )
        fill_drawing.setChecked(True)

        # Label list context menu.
        self.labelMenu = QtWidgets.QMenu()
        addActions(self.labelMenu, (edit, delete))
        self.labelList.setContextMenuPolicy(Qt.CustomContextMenu)
        self.labelList.customContextMenuRequested.connect(
            self.popLabelListMenu)
        
        # Store actions for further handling.
        self.actions = struct(
            saveAuto=saveAuto,
            changeOutputDir=changeOutputDir,
            save=save, saveAs=saveAs, open=open_, close=close,
            lineColor=color1, fillColor=color2,
            toggleKeepPrevMode=toggle_keep_prev_mode,
            delete=delete, edit=edit, copy=copy,
            undoLastPoint=undoLastPoint, undo=undo, 
            addPoint=addPoint,
            createMode=createMode, editMode=editMode,
            createRectangleMode=createRectangleMode,
            createCircleMode=createCircleMode,
            createLineMode=createLineMode,
            createPointMode=createPointMode,
            createLineStripMode=createLineStripMode,
            shapeLineColor=shapeLineColor, shapeFillColor=shapeFillColor,
            # zoom=zoom, zoomIn=zoomIn, zoomOut=zoomOut, zoomOrg=zoomOrg, *
            # fitWindow=fitWindow, fitWidth=fitWidth,
            # zoomActions=zoomActions,
            openNextImg=openNextImg, openPrevImg=openPrevImg,
            fileMenuActions=(open_, opendir, save, saveAs, close, quit),
            tool=(),
            editMenu=(edit, copy, delete, None, undo, #undoLastPoint,
                      None, color1, color2, None, toggle_keep_prev_mode),
            # menu shown at right click
            menu=(
                createMode,
                createRectangleMode,
                createCircleMode,
                createLineMode,
                createPointMode,
                createLineStripMode,
                editMode,
                edit,
                copy,
                delete,
                shapeLineColor,
                shapeFillColor,
                undo,
                undoLastPoint,
                addPoint,
            ),
            onLoadActive=(
                close,
                createMode,
                createRectangleMode,
                createCircleMode,
                createLineMode,
                createPointMode,
                createLineStripMode,
                editMode,
            ),
            onShapesPresent=(saveAs, hideAll, showAll),
        )
        # Menu buttons on Left
        self.actions.tool = (
            open_,
            opendir,
            openNextImg,
            openPrevImg,
            save,
            None,
            createMode,
            editMode,
            copy,
            delete,
            undo,
            None,
            #zoomIn, *
            #zoom,
            #zoomOut,
            #fitWindow,
            #fitWidth,
        )
        # Custom context menu for the canvas widget:
        addActions(self.canvasMenus[0], self.actions.menu)
        addActions(self.canvasMenus[1], (
            action('&拷贝到这里', self.copyShape),
            action('&移动到这里', self.moveShape)))

        self.menus = struct(
            file=self.menu('&文件'),
            edit=self.menu('&编辑'),
            view=self.menu('&视图'),
            help=self.menu('&帮助'),
            recentFiles=QtWidgets.QMenu('打开 &最近文件'),
            labelList=self.labelMenu,
        )
        addActions(self.menus.file, (open_, openNextImg, openPrevImg, opendir,
                                     self.menus.recentFiles,
                                     save, saveAs, saveAuto, changeOutputDir,
                                     close,
                                     None,
                                     quit))
        addActions(self.menus.help, (help,))
        addActions(self.menus.view, (
            self.flag_dock.toggleViewAction(),
            self.label_dock.toggleViewAction(),
            self.shape_dock.toggleViewAction(),
            self.file_dock.toggleViewAction(),
            None,
            fill_drawing,
            None,
            hideAll,
            showAll,
            None,
            #zoomIn,
            #zoomOut,
            #zoomOrg,
            None,
            #fitWindow,
            #fitWidth,
            None,
        ))
        self.menus.file.aboutToShow.connect(self.updateFileMenu)

    def setSignals(self):
        '''      
        #connect signal

        if self.output_file is not None and self._config['auto_save']:
            logger.warn(
                'If `auto_save` argument is True, `output_file` argument '
                'is ignored and output filename is automatically '
                'set as IMAGE_BASENAME.json.'
            )
        '''
        # this signal is export from riverMon
        self.editor.drawingPolygon.connect(self.toggleDrawingSensitive)
        self.editor.newShape.connect(self.newShape)
        self.editor.shapeMoved.connect(self.setDirty)
        self.editor.selectionChanged.connect(self.shapeSelectionChanged)
        self.editor.editorClose.connect(self.closeEvent)
        self.editor.enabled.connect(self.editorEnabled)
        self.editor.edgeSelected.connect(self.actions.addPoint.setEnabled)

    ########################################################################################################
    #                                         Utils
    ########################################################################################################
    def errorMessage(self, title, message):
        return QtWidgets.QMessageBox.critical(
            self, title, '<p><b>%s</b></p>%s' % (title, message))
    
    def statusBar(self):
        return self.mainWnd.statusBar()

    def status(self, message, delay=5000):
        self.mainWnd.statusBar().showMessage(message, delay)
    
    
    def popLabelListMenu(self, point):
        self.menus.labelList.exec_(self.labelList.mapToGlobal(point))

    def addRecentFile(self, filename):
        if filename in self.recentFiles:
            self.recentFiles.remove(filename)
        elif len(self.recentFiles) >= self.maxRecent:
            self.recentFiles.pop()
        self.recentFiles.insert(0, filename)

    def adjustScale(self, initial=False):
        '''
        value = self.scalers[self.FIT_WINDOW if initial else self.zoomMode]()
        self.zoomWidget.setValue(int(100 * value))
        '''
        pass 

    def paintCanvas(self):
        '''
        assert not self.image.isNull(), "cannot paint null image"
        self.canvas.scale = 0.01 * self.zoomWidget.value()
        self.canvas.adjustSize()
        self.canvas.update()  
        '''
        self.iface.openFile(self.filename)
        pass

    def toggleActions(self, value=True):
        """Enable/Disable widgets which depend on an opened image."""
        '''
        for z in self.actions.zoomActions:
           z.setEnabled(value)
        '''
        for action in self.actions.onLoadActive:
            action.setEnabled(value)

    def scanAllImages(self, folderPath):
        extensions = self.supportedFmts
        images = []

        for root, dirs, files in os.walk(folderPath):
            for file in files:
                if file.lower().endswith(tuple(extensions)):
                    relativePath = osp.join(root, file)
                    images.append(relativePath)
        images.sort(key=lambda x: x.lower())
        return images

    def validateLabel(self, label):
        # no validation
        if self._config['validate_label'] is None:
            return True

        for i in range(self.uniqLabelList.count()):
            label_i = self.uniqLabelList.item(i).text()
            if self._config['validate_label'] in ['exact', 'instance']:
                if label_i == label:
                    return True
            if self._config['validate_label'] == 'instance':
                m = re.match(r'^{}-[0-9]*$'.format(label_i), label)
                if m:
                    return True
        return False

    def currentItem(self):
        items = self.labelList.selectedItems()
        if items:
            return items[0]
        return None

    def labelSelectionChanged(self):
        item = self.currentItem()
        if item and self.editor.isEditing():
            self._noSelectionSlot = True
            shape = self.labelList.get_shape_from_item(item)
            self.editor.selectShape(shape)

    def labelItemChanged(self, item):
        print('* labelItemChanged')
        shape = self.labelList.get_shape_from_item(item)
        print('* shape type is ', type(shape))
        label = str(item.text())
        if label != shape.getLabel():
            print('* shape label is not equal, shape.label={}'.format(shape.getLabel()))
            #shape.label = str(item.text())
            shape.setLabel(str(item.text()))
            self.setDirty()
        else:  # User probably changed item visibility
            print('*set visible') 
            self.editor.setShapeVisible(shape, item.checkState() == Qt.Checked)
    
    def loadRecent(self, filename):
        if self.mayContinue():
            self.loadFile(filename)

    def updateFileMenu(self):
        current = self.filename

        def exists(filename):
            return osp.exists(str(filename))

        menu = self.menus.recentFiles
        menu.clear()
        files = [f for f in self.recentFiles if f != current and exists(f)]
        for i, f in enumerate(files):
            icon = newIcon('labels')
            action = QtWidgets.QAction(
                icon, '&%d %s' % (i + 1, QtCore.QFileInfo(f).fileName()), self.mainWnd)
            action.triggered.connect(functools.partial(self.loadRecent, f))
            menu.addAction(action)

    def mouseRelease(self,ev):
        if ev.button() == QtCore.Qt.RightButton:
            if self.editor.canBreak():
                menu = self.canvasMenus[0]
                menu.exec_(self.canvas.mapToGlobal(ev.pos()))           
        return True

    def editorEnabled(self, value):
        if(value):
            print('** editor is enabled from HOST')
        else:
            print('** editor is disabled from HOST')
        self.toggleActions(True)


    def closeEvent(self):
        print('*^^^^^^^^^^^^^^^close')
        if not self.mayContinue():
            event.ignore()
        self.settings.setValue(
            'filename', self.filename if self.filename else '')
        self.settings.setValue('window/size', self.mainWnd.size())
        self.settings.setValue('window/position', self.mainWnd.pos())
        self.settings.setValue('window/state', self.mainWnd.saveState())
        self.settings.setValue('line/color', self.lineColor)
        self.settings.setValue('fill/color', self.fillColor)
        self.settings.setValue('recentFiles', self.recentFiles)
        # ask the use for where to save the labels
        # self.settings.setValue('window/geometry', self.saveGeometry())
########################################################################################################
#                                          GDAL                                        
########################################################################################################
import numpy as np
def read(filename):
    img = None
    try:
        img = gdal.Open(filename)
        datatype = img.GetRasterBand(1).DataType
        print('* datatype', datatype) 
        '''
        desc = img.GetDescription()
        metadata = img.GetMetadata() #
        print('*Raster description: {desc}'.format(desc=desc))
        print('*Raster metadata:')
        print(metadata) # {'AREA_OR_POINT': 'Area'}
        print('\n')
        '''
        if (datatype != 1):
            bandNum = img.RasterCount
            for bandIdx in np.arange(1, bandNum+1):
                band = img.GetRasterBand(int(bandIdx))
                stats = band.GetStatistics(0,1) #if no statistic , it will compute
    except Exception:
        print('*gdal read {}, failed'.format(filename))
        exstr = traceback.format_exc()
        print (exstr)
    return img 
        

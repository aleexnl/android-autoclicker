import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase, QFont, QPalette, QColor
from app import Editor
app = QApplication([])
QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
app.setFont(QFont('Segoe UI', 10))
w = Editor(startup=False)
w.show()
app.processEvents()
w.grab().save('artifacts/ui/after.png')
w.resize(850, 600)
w.log_toggle.setChecked(False)
app.processEvents()
w.grab().save('artifacts/ui/compact.png')
# Exercise inheritance from a dark system palette on the offscreen platform.
p = QPalette()
for role, value in [('Window','#202124'),('WindowText','#eeeeee'),('Base','#17181a'),('AlternateBase','#26282c'),('Text','#eeeeee'),('Button','#303238'),('ButtonText','#eeeeee'),('Highlight','#276b9b'),('HighlightedText','#ffffff'),('PlaceholderText','#aeb4bd')]:
    p.setColor(getattr(QPalette.ColorRole,role),QColor(value))
w.close()
app.setStyle("Fusion")
app.setPalette(p)
w = Editor(startup=False)
w.show()
w.resize(1200,850)
app.processEvents()
w.grab().save('artifacts/ui/dark.png')
w.close()

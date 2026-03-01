import os
import sys
from PyQt6.QtWidgets import QApplication, QStyleFactory, QMainWindow, QVBoxLayout, QWidget, QLabel

def main() -> None:
    app = QApplication(sys.argv)
    
    print("\n" + "="*40)
    print(" NativMix Theme Debugger")
    print("="*40)
    
    vars_to_check = [
        "XDG_CURRENT_DESKTOP", 
        "XDG_SESSION_TYPE",
        "QT_QPA_PLATFORMTHEME", 
        "QT_STYLE_OVERRIDE", 
        "QT_QPA_PLATFORM"
    ]
    
    print("\n[Environment Variables]")
    for var in vars_to_check:
        print(f"  {var:<22} = {os.environ.get(var, 'NOT SET')}")
        
    styles = QStyleFactory.keys()
    print("\n[Qt Styles]")
    print(f"  Available styles       = {styles}")
    print(f"  Currently Active Style = {app.style().objectName()}")
    print("="*40 + "\n")
    
    # Simple window to visually test the Qt Application
    win = QMainWindow()
    win.setWindowTitle("NativMix Theme Debugger")
    
    central = QWidget()
    layout = QVBoxLayout(central)
    
    layout.addWidget(QLabel("<b>Environment Variables:</b>"))
    for var in vars_to_check:
        val = os.environ.get(var, '<i>NOT SET</i>')
        layout.addWidget(QLabel(f"{var}: {val}"))
        
    layout.addWidget(QLabel("<br><b>Qt Styles:</b>"))
    layout.addWidget(QLabel(f"Available: {', '.join(styles)}"))
    layout.addWidget(QLabel(f"Active: <b>{app.style().objectName()}</b>"))
    
    win.setCentralWidget(central)
    win.setMinimumSize(400, 300)
    win.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

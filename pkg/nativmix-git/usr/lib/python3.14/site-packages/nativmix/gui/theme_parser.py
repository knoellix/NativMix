"""
KDE Color Scheme Parser for NativMix.

Reads standard `.colors` INI files and translates them into a QPalette.
"""

import os
import configparser
import logging
from pathlib import Path
from PyQt6.QtGui import QPalette, QColor

logger = logging.getLogger(__name__)

KDE_THEME_DIRS = [
    Path(os.path.expanduser("~/.local/share/color-schemes")),
    Path("/usr/share/color-schemes")
]

def discover_kde_schemes() -> dict[str, str]:
    """
    Scan KDE directories for .colors files.
    Returns a dict mapping Theme Name -> Absolute File Path.
    """
    schemes = {}
    
    for d in KDE_THEME_DIRS:
        if not d.exists() or not d.is_dir():
            continue
            
        for filepath in d.glob("*.colors"):
            try:
                parser = configparser.ConfigParser()
                parser.read(filepath, encoding='utf-8')
                
                name = filepath.stem
                if parser.has_section("General") and parser.has_option("General", "Name"):
                    name = parser.get("General", "Name")
                    
                schemes[name] = str(filepath.absolute())
            except Exception as e:
                logger.debug(f"Failed to parse {filepath.name} for name: {e}")
                
    return schemes

def parse_kde_scheme(filepath: str) -> QPalette | None:
    """
    Parse a KDE .colors file and construct a QPalette.
    Returns None if the file could not be parsed.
    """
    path = Path(filepath)
    if not path.exists():
        return None
        
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding='utf-8')
    except Exception as e:
        logger.error(f"Failed to read color scheme {path.name}: {e}")
        return None
        
    palette = QPalette()
    
    # Helper to safely parse "R,G,B" strings
    def get_color(section: str, key: str) -> QColor | None:
        if not parser.has_section(section) or not parser.has_option(section, key):
            return None
        valStr = parser.get(section, key)
        try:
            parts = [int(p.strip()) for p in valStr.split(',')]
            if len(parts) >= 3:
                return QColor(parts[0], parts[1], parts[2])
        except ValueError:
            pass
        return None
        
    try:
        # [Colors:Window]
        c = get_color("Colors:Window", "BackgroundNormal")
        if c: palette.setColor(QPalette.ColorRole.Window, c)
        
        c = get_color("Colors:Window", "ForegroundNormal")
        if c: palette.setColor(QPalette.ColorRole.WindowText, c)
        
        # [Colors:View]
        c = get_color("Colors:View", "BackgroundNormal")
        if c: palette.setColor(QPalette.ColorRole.Base, c)
        
        c = get_color("Colors:View", "ForegroundNormal")
        if c: palette.setColor(QPalette.ColorRole.Text, c)
        
        # [Colors:Button]
        c = get_color("Colors:Button", "BackgroundNormal")
        if c: palette.setColor(QPalette.ColorRole.Button, c)
        
        c = get_color("Colors:Button", "ForegroundNormal")
        if c: palette.setColor(QPalette.ColorRole.ButtonText, c)
        
        # [Colors:Selection]
        c = get_color("Colors:Selection", "BackgroundNormal")
        if not c:
            # Fallback to DecorationFocus if BackgroundNormal is missing
            c = get_color("Colors:Selection", "DecorationFocus")
        if c: palette.setColor(QPalette.ColorRole.Highlight, c)
        
        c = get_color("Colors:Selection", "ForegroundNormal")
        if c: palette.setColor(QPalette.ColorRole.HighlightedText, c)
        
        # [Colors:Window] Links
        c = get_color("Colors:Window", "ForegroundLink")
        if c: palette.setColor(QPalette.ColorRole.Link, c)
        
        c = get_color("Colors:Window", "ForegroundVisited")
        if c: palette.setColor(QPalette.ColorRole.LinkVisited, c)
        
        # [Colors:Tooltip]
        c_bg = get_color("Colors:Tooltip", "BackgroundNormal")
        c_fg = get_color("Colors:Tooltip", "ForegroundNormal")
        
        if c_bg: 
            palette.setColor(QPalette.ColorRole.ToolTipBase, c_bg)
        else:
            palette.setColor(QPalette.ColorRole.ToolTipBase, palette.color(QPalette.ColorRole.Window))
            
        if c_fg: 
            palette.setColor(QPalette.ColorRole.ToolTipText, c_fg)
        else:
            palette.setColor(QPalette.ColorRole.ToolTipText, palette.color(QPalette.ColorRole.WindowText))

    except Exception as e:
        logger.error(f"Error mapping colors for {path.name}: {e}")
        # Return whatever we managed to parse so far rather than crashing
        
    return palette

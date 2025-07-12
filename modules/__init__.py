import os
import sys
import importlib

__all__ = ["load_modules"]

def load_modules(agent):
    """Dynamically load all module classes in this package and instantiate them."""
    modules = []
    
    # This logic makes the path work in both normal execution and as a PyInstaller .exe
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # Running in a PyInstaller bundle
        base_dir = os.path.join(sys._MEIPASS, 'modules')
    else:
        # Running in a normal Python environment
        base_dir = os.path.dirname(os.path.abspath(__file__))

    modules_to_exclude = [
        '__init__.py', 
        'base.py', 
        'adventure.py', 
        'hero.py', 
        'training.py', 
        'demolish.py',
        'smithyupgrades.py',
        'loop.py'
    ]

    for fname in sorted(os.listdir(base_dir)):
        if fname.endswith('.py') and fname not in modules_to_exclude:
            mod_name = fname[:-3]
            try:
                mod = importlib.import_module(f'modules.{mod_name}')
                cls = getattr(mod, 'Module', None)
                if cls:
                    modules.append(cls(agent))
            except Exception as e:
                # Add logging for any potential import errors
                print(f"Failed to load module {mod_name}: {e}")

    return modules
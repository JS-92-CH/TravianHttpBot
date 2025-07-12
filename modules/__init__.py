# modules/__init__.py

import os
import importlib

__all__ = ["load_modules"]

def load_modules(agent):
    """Dynamically load all module classes in this package and instantiate them."""
    modules = []
    base_dir = os.path.dirname(__file__)
    # Exclude independent agents (threads) and base classes from this list.
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
            mod = importlib.import_module(f'.{mod_name}', package=__name__)
            cls = getattr(mod, 'Module', None)
            if cls:
                modules.append(cls(agent))
    return modules
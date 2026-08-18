"""
joydigi/inherit/

Extension infrastructure for Joydigi — model field injection and CBV replacement.

Key public symbols re-exported here for convenience:

    from joydigi.inherit import JoydigiViewInheritMixin   # view extension
    from joydigi.inherit import JoydigiModelBase           # model metaclass
    from joydigi.inherit import INJECTION_MAP              # migration routing
    from joydigi.inherit import VIEW_REGISTRY              # registered views
"""

from joydigi.inherit.extension_registry import INJECTION_MAP
from joydigi.inherit.model_inherit import EXTENSION_REGISTRY, JoydigiModelBase
from joydigi.inherit.view_inherit import JoydigiViewInheritMixin
from joydigi.inherit.view_registry import VIEW_REGISTRY

__all__ = [
    "JoydigiViewInheritMixin",
    "JoydigiModelBase",
    "INJECTION_MAP",
    "EXTENSION_REGISTRY",
    "VIEW_REGISTRY",
]

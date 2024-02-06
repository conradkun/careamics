"""Main CAREamics module."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("careamics")
except PackageNotFoundError:
    __version__ = "uninstalled"

__all__ = [
    "CAREamist", 
    "CAREamicsKiln",
    "CAREamicsModule", 
    "Configuration", 
    "load_configuration", 
    "save_configuration"
]

from .config import Configuration, load_configuration, save_configuration
from .careamist import CAREamist
from .lightning_module import (
    CAREamicsKiln, 
    CAREamicsModule,
    CAREamicsTrainDataModule,
    CAREamicsPredictDataModule
)

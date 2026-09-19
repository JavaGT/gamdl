import configparser
import os
import stat
import tempfile
import typing
from functools import wraps
from pathlib import Path

import click
import click.types as click_types

from .cli_config import CliConfig
from .constants import EXCLUDED_CONFIG_FILE_PARAMS
from .utils import Csv

# Old config key -> renamed key. The value under the old key is applied once
# and written back under the new key, with a warning to update the config.
RENAMED_PARAMS = {
    "song_codec_piority": "song_codec_priority",
}


class ConfigFile:
    # Warnings collected while loading the config file, emitted by the CLI
    # once logging is configured.
    pending_warnings: list[str] = []

    def __init__(
        self,
        config_path: str,
        section_name: str = "gamdl",
    ) -> None:
        self.config_path = config_path
        self.section_name = section_name

        self.click_context = click.get_current_context()
        self._read_config_file()

    def _read_config_file(self) -> None:
        self.config = configparser.ConfigParser(interpolation=None)

        if Path(self.config_path).exists():
            self.config.read(self.config_path, encoding="utf-8")
        else:
            Path(self.config_path).parent.mkdir(parents=True, exist_ok=True)

        if not self.config.has_section(self.section_name):
            self.config.add_section(self.section_name)

    def _write_config_file(self) -> None:
        if (
            Path(self.config_path).exists()
            and not os.access(self.config_path, os.W_OK)
        ):
            self._warn(
                f"Could not write config file '{self.config_path}': "
                "file is not writable"
            )
            return
        try:
            fd, temp_path = tempfile.mkstemp(
                dir=Path(self.config_path).parent,
                prefix=f"{Path(self.config_path).name}.",
                suffix=".tmp",
            )
            if Path(self.config_path).exists():
                os.chmod(
                    temp_path,
                    stat.S_IMODE(os.stat(self.config_path).st_mode),
                )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as config_file:
                    self.config.write(config_file)
                    config_file.flush()
                    os.fsync(config_file.fileno())
                os.replace(temp_path, self.config_path)
            except BaseException:
                # close the mkstemp fd if fdopen itself failed; already
                # closed once the context manager exited
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            self._warn(f"Could not write config file '{self.config_path}': {e}")

    def _warn(self, message: str) -> None:
        ConfigFile.pending_warnings.append(message)

    def _serialize_param_default(self, param: click.Parameter) -> str:
        if param.default is None:
            return "null"

        if isinstance(param.type, Csv):
            return ",".join(
                item.value if hasattr(item, "value") else str(item)
                for item in param.default
            )

        if isinstance(param.type, click_types.FuncParamType):
            return param.default.value

        if isinstance(param.type, click_types.BoolParamType):
            return "true" if param.default else "false"

        if isinstance(
            param.type,
            click_types.Choice
            | click_types.Path
            | click_types.StringParamType
            | click_types.IntParamType,
        ):
            return str(param.default)

        raise NotImplementedError(
            f"Serialization for parameter '{param.name}' of type "
            f"'{type(param.type)}' is not implemented."
        )

    def _add_param_default_to_config(
        self,
        param: click.Parameter,
    ) -> bool:
        if self.config.has_option(self.section_name, param.name):
            return False

        value = self._serialize_param_default(param)
        self.config.set(self.section_name, param.name, value)

        return True

    def _parse_param_from_config(
        self,
        param: click.Parameter,
    ) -> typing.Any:
        value = self.config[self.section_name].get(param.name)
        if value is None:
            return param.default

        if value == "null":
            return None

        if not isinstance(param.type, click_types.ParamType):
            raise NotImplementedError(
                f"Parsing for parameter '{param.name}' of type "
                f"'{type(param.type)}' is not implemented."
            )

        return param.type.convert(value, None, None)

    def add_params_default_to_config(self) -> None:
        has_changes = False

        for param in self.click_context.command.params:
            if param.name in EXCLUDED_CONFIG_FILE_PARAMS:
                continue

            has_changes = self._add_param_default_to_config(param) or has_changes

        if has_changes:
            self._write_config_file()

    def _apply_renamed_params(self) -> None:
        has_changes = False
        for old_name, new_name in RENAMED_PARAMS.items():
            if not self.config.has_option(self.section_name, old_name):
                continue

            warning = (
                f"Config option '{old_name}' was renamed to '{new_name}', "
                "update your config file"
            )
            if not self.config.has_option(self.section_name, new_name):
                value = self.config[self.section_name].get(old_name)
                self.config.set(self.section_name, new_name, value)
                self._warn(warning)
            else:
                self._warn(f"{warning}, keeping the '{new_name}' value")

            self.config.remove_option(self.section_name, old_name)
            has_changes = True

        if has_changes:
            self._write_config_file()

    def cleanup_unknown_params(self) -> None:
        param_names = {info.name for info in self.click_context.command.params}

        unknown_keys = [
            key
            for key in self.config[self.section_name].keys()
            if key not in param_names
        ]
        for key in unknown_keys:
            self._warn(f"Unknown config option '{key}', removing it")
            self.config.remove_option(self.section_name, key)

        if unknown_keys:
            self._write_config_file()

    def update_params_from_config(self) -> None:
        for param in self.click_context.command.params:
            if (
                self.click_context.get_parameter_source(param.name)
                == click.core.ParameterSource.COMMANDLINE
            ):
                continue

            if self.config.has_option(self.section_name, param.name):
                self.click_context.params[param.name] = self._parse_param_from_config(
                    param
                )

    def get_cli_config(self) -> CliConfig:
        config_dict = {}
        for param in self.click_context.command.params:
            if param.name in {"help", "version"}:
                continue

            config_dict[param.name] = self.click_context.params.get(
                param.name, param.default
            )
        return CliConfig(**config_dict)

    def load(self) -> CliConfig:
        ConfigFile.pending_warnings = []
        self._apply_renamed_params()
        self.cleanup_unknown_params()
        self.add_params_default_to_config()
        self.update_params_from_config()
        return self.get_cli_config()

    @staticmethod
    def loader(func):
        @wraps(func)
        def wrapper(cli_config: CliConfig):
            ctx = click.get_current_context()
            config_path = ctx.params.get("config_path")
            no_config_file = ctx.params.get("no_config_file")
            if config_path and not no_config_file:
                cli_config = ConfigFile(config_path).load()
            return func(cli_config)

        return wrapper

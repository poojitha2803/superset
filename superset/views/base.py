# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import dataclasses
import functools
import logging
import os
import traceback
from datetime import datetime
from typing import Any, Callable, cast

import simplejson as json
import yaml
from flask import (
    abort,
    flash,
    g,
    get_flashed_messages,
    redirect,
    request,
    Response,
    send_file,
    session,
)
from flask_appbuilder import BaseView, Model, ModelView
from flask_appbuilder.actions import action
from flask_appbuilder.forms import DynamicForm
from flask_appbuilder.models.sqla.filters import BaseFilter
from flask_appbuilder.security.sqla.models import User
from flask_appbuilder.widgets import ListWidget
from flask_babel import get_locale, gettext as __, lazy_gettext as _
from flask_jwt_extended.exceptions import NoAuthorizationError
from flask_wtf.csrf import CSRFError
from flask_wtf.form import FlaskForm
from pkg_resources import resource_filename
from sqlalchemy import exc
from sqlalchemy.orm import Query
from werkzeug.exceptions import HTTPException
from wtforms import Form
from wtforms.fields.core import Field, UnboundField

from superset import (
    app as superset_app,
    appbuilder,
    conf,
    get_feature_flags,
    is_feature_enabled,
    security_manager,
)
from superset.commands.exceptions import CommandException, CommandInvalidError
from superset.connectors.sqla import models
from superset.db_engine_specs import get_available_engine_specs
from superset.db_engine_specs.gsheets import GSheetsEngineSpec
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    SupersetErrorException,
    SupersetErrorsException,
    SupersetException,
    SupersetSecurityException,
)
from superset.extensions import cache_manager
from superset.models.helpers import ImportExportMixin
from superset.reports.models import ReportRecipientType
from superset.superset_typing import FlaskResponse
from superset.translations.utils import get_language_pack
from superset.utils import core as utils
from superset.utils.filters import get_dataset_access_filters

from .utils import bootstrap_user_data

FRONTEND_CONF_KEYS = (
    "SUPERSET_WEBSERVER_TIMEOUT",
    "SUPERSET_DASHBOARD_POSITION_DATA_LIMIT",
    "SUPERSET_DASHBOARD_PERIODICAL_REFRESH_LIMIT",
    "SUPERSET_DASHBOARD_PERIODICAL_REFRESH_WARNING_MESSAGE",
    "DISABLE_DATASET_SOURCE_EDIT",
    "ENABLE_JAVASCRIPT_CONTROLS",
    "DEFAULT_SQLLAB_LIMIT",
    "DEFAULT_VIZ_TYPE",
    "SQL_MAX_ROW",
    "SUPERSET_WEBSERVER_DOMAINS",
    "SQLLAB_SAVE_WARNING_MESSAGE",
    "DISPLAY_MAX_ROW",
    "GLOBAL_ASYNC_QUERIES_TRANSPORT",
    "GLOBAL_ASYNC_QUERIES_POLLING_DELAY",
    "SQL_VALIDATORS_BY_ENGINE",
    "SQLALCHEMY_DOCS_URL",
    "SQLALCHEMY_DISPLAY_TEXT",
    "GLOBAL_ASYNC_QUERIES_WEBSOCKET_URL",
    "DASHBOARD_AUTO_REFRESH_MODE",
    "DASHBOARD_AUTO_REFRESH_INTERVALS",
    "DASHBOARD_VIRTUALIZATION",
    "SCHEDULED_QUERIES",
    "EXCEL_EXTENSIONS",
    "CSV_EXTENSIONS",
    "COLUMNAR_EXTENSIONS",
    "ALLOWED_EXTENSIONS",
    "SAMPLES_ROW_LIMIT",
    "DEFAULT_TIME_FILTER",
    "HTML_SANITIZATION",
    "HTML_SANITIZATION_SCHEMA_EXTENSIONS",
    "WELCOME_PAGE_LAST_TAB",
    "VIZ_TYPE_DENYLIST",
    "ALERT_REPORTS_DEFAULT_CRON_VALUE",
    "ALERT_REPORTS_DEFAULT_RETENTION",
    "ALERT_REPORTS_DEFAULT_WORKING_TIMEOUT",
    "NATIVE_FILTER_DEFAULT_ROW_LIMIT",
    "PREVENT_UNSAFE_DEFAULT_URLS_ON_DATASET",
)

logger = logging.getLogger(__name__)
config = superset_app.config


def get_error_msg() -> str:
    if conf.get("SHOW_STACKTRACE"):
        error_msg = traceback.format_exc()
    else:
        error_msg = "FATAL ERROR \n"
        error_msg += (
            "Stacktrace is hidden. Change the SHOW_STACKTRACE "
            "configuration setting to enable it"
        )
    return error_msg


def json_error_response(
    msg: str | None = None,
    status: int = 500,
    payload: dict[str, Any] | None = None,
    link: str | None = None,
) -> FlaskResponse:
    if not payload:
        payload = {"error": f"{msg}"}
    if link:
        payload["link"] = link

    return Response(
        json.dumps(payload, default=utils.json_iso_dttm_ser, ignore_nan=True),
        status=status,
        mimetype="application/json",
    )


def json_errors_response(
    errors: list[SupersetError],
    status: int = 500,
    payload: dict[str, Any] | None = None,
) -> FlaskResponse:
    if not payload:
        payload = {}

    payload["errors"] = [dataclasses.asdict(error) for error in errors]
    return Response(
        json.dumps(payload, default=utils.json_iso_dttm_ser, ignore_nan=True),
        status=status,
        mimetype="application/json; charset=utf-8",
    )


def json_success(json_msg: str, status: int = 200) -> FlaskResponse:
    return Response(json_msg, status=status, mimetype="application/json")


def data_payload_response(payload_json: str, has_error: bool = False) -> FlaskResponse:
    status = 400 if has_error else 200
    return json_success(payload_json, status=status)


def generate_download_headers(
    extension: str, filename: str | None = None
) -> dict[str, Any]:
    filename = filename if filename else datetime.now().strftime("%Y%m%d_%H%M%S")
    content_disp = f"attachment; filename={filename}.{extension}"
    headers = {"Content-Disposition": content_disp}
    return headers


def deprecated(
    eol_version: str = "4.0.0",
    new_target: str | None = None,
) -> Callable[[Callable[..., FlaskResponse]], Callable[..., FlaskResponse]]:
    """
    A decorator to set an API endpoint from SupersetView has deprecated.
    Issues a log warning
    """

    def _deprecated(f: Callable[..., FlaskResponse]) -> Callable[..., FlaskResponse]:
        def wraps(self: BaseSupersetView, *args: Any, **kwargs: Any) -> FlaskResponse:
            messsage = (
                "%s.%s "
                "This API endpoint is deprecated and will be removed in version %s"
            )
            logger_args = [
                self.__class__.__name__,
                f.__name__,
                eol_version,
            ]
            if new_target:
                messsage += " . Use the following API endpoint instead: %s"
                logger_args.append(new_target)
            logger.warning(messsage, *logger_args)
            return f(self, *args, **kwargs)

        return functools.update_wrapper(wraps, f)

    return _deprecated


def api(f: Callable[..., FlaskResponse]) -> Callable[..., FlaskResponse]:
    """
    A decorator to label an endpoint as an API. Catches uncaught exceptions and
    return the response in the JSON format
    """

    def wraps(self: BaseSupersetView, *args: Any, **kwargs: Any) -> FlaskResponse:
        try:
            return f(self, *args, **kwargs)
        except NoAuthorizationError:
            logger.warning("Api failed- no authorization", exc_info=True)
            return json_error_response(get_error_msg(), status=401)
        except Exception as ex:  # pylint: disable=broad-except
            logger.exception(ex)
            return json_error_response(get_error_msg())

    return functools.update_wrapper(wraps, f)


def handle_api_exception(
    f: Callable[..., FlaskResponse]
) -> Callable[..., FlaskResponse]:
    """
    A decorator to catch superset exceptions. Use it after the @api decorator above
    so superset exception handler is triggered before the handler for generic
    exceptions.
    """

    def wraps(self: BaseSupersetView, *args: Any, **kwargs: Any) -> FlaskResponse:
        try:
            return f(self, *args, **kwargs)
        except SupersetSecurityException as ex:
            logger.warning("SupersetSecurityException", exc_info=True)
            return json_errors_response(
                errors=[ex.error], status=ex.status, payload=ex.payload
            )
        except SupersetErrorsException as ex:
            logger.warning(ex, exc_info=True)
            return json_errors_response(errors=ex.errors, status=ex.status)
        except SupersetErrorException as ex:
            logger.warning("SupersetErrorException", exc_info=True)
            return json_errors_response(errors=[ex.error], status=ex.status)
        except SupersetException as ex:
            if ex.status >= 500:
                logger.exception(ex)
            return json_error_response(
                utils.error_msg_from_exception(ex), status=ex.status
            )
        except HTTPException as ex:
            logger.exception(ex)
            return json_error_response(
                utils.error_msg_from_exception(ex), status=cast(int, ex.code)
            )
        except (exc.IntegrityError, exc.DatabaseError, exc.DataError) as ex:
            logger.exception(ex)
            return json_error_response(utils.error_msg_from_exception(ex), status=422)
        except Exception as ex:  # pylint: disable=broad-except
            logger.exception(ex)
            return json_error_response(utils.error_msg_from_exception(ex))

    return functools.update_wrapper(wraps, f)


class BaseSupersetView(BaseView):
    @staticmethod
    def json_response(obj: Any, status: int = 200) -> FlaskResponse:
        return Response(
            json.dumps(obj, default=utils.json_int_dttm_ser, ignore_nan=True),
            status=status,
            mimetype="application/json",
        )

    def render_app_template(self) -> FlaskResponse:
        payload = {
            "user": bootstrap_user_data(g.user, include_perms=True),
            "common": common_bootstrap_payload(),
        }
        return self.render_template(
            "superset/spa.html",
            entry="spa",
            bootstrap_data=json.dumps(
                payload, default=utils.pessimistic_json_iso_dttm_ser
            ),
        )


def get_environment_tag() -> dict[str, Any]:
    # Whether flask is in debug mode (--debug)
    debug = appbuilder.app.config["DEBUG"]

    # Getting the configuration option for ENVIRONMENT_TAG_CONFIG
    env_tag_config = appbuilder.app.config["ENVIRONMENT_TAG_CONFIG"]

    # These are the predefined templates define in the config
    env_tag_templates = env_tag_config.get("values")

    # This is the environment variable name from which to select the template
    # default is SUPERSET_ENV (from FLASK_ENV in previous versions)
    env_envvar = env_tag_config.get("variable")

    # this is the actual name we want to use
    env_name = os.environ.get(env_envvar)

    if not env_name or env_name not in env_tag_templates.keys():
        env_name = "debug" if debug else None

    env_tag = env_tag_templates.get(env_name)
    return env_tag or {}


def menu_data(user: User) -> dict[str, Any]:
    menu = appbuilder.menu.get_data()

    languages = {}
    for lang in appbuilder.languages:
        languages[lang] = {
            **appbuilder.languages[lang],
            "url": appbuilder.get_url_for_locale(lang),
        }
    brand_text = appbuilder.app.config["LOGO_RIGHT_TEXT"]
    if callable(brand_text):
        brand_text = brand_text()
    build_number = appbuilder.app.config["BUILD_NUMBER"]

    return {
        "menu": menu,
        "brand": {
            "path": appbuilder.app.config["LOGO_TARGET_PATH"] or "/superset/welcome/",
            "icon": appbuilder.app_icon,
            "alt": appbuilder.app_name,
            "tooltip": appbuilder.app.config["LOGO_TOOLTIP"],
            "text": brand_text,
        },
        "environment_tag": get_environment_tag(),
        "navbar_right": {
            # show the watermark if the default app icon has been overridden
            "show_watermark": ("superset-logo-horiz" not in appbuilder.app_icon),
            "bug_report_url": appbuilder.app.config["BUG_REPORT_URL"],
            "bug_report_icon": appbuilder.app.config["BUG_REPORT_ICON"],
            "bug_report_text": appbuilder.app.config["BUG_REPORT_TEXT"],
            "documentation_url": appbuilder.app.config["DOCUMENTATION_URL"],
            "documentation_icon": appbuilder.app.config["DOCUMENTATION_ICON"],
            "documentation_text": appbuilder.app.config["DOCUMENTATION_TEXT"],
            "version_string": appbuilder.app.config["VERSION_STRING"],
            "version_sha": appbuilder.app.config["VERSION_SHA"],
            "build_number": build_number,
            "languages": languages,
            "show_language_picker": len(languages.keys()) > 1,
            "user_is_anonymous": user.is_anonymous,
            "user_info_url": None
            if is_feature_enabled("MENU_HIDE_USER_INFO")
            else appbuilder.get_url_for_userinfo,
            "user_logout_url": appbuilder.get_url_for_logout,
            "user_login_url": appbuilder.get_url_for_login,
            "user_profile_url": None
            if user.is_anonymous or is_feature_enabled("MENU_HIDE_USER_INFO")
            else "/superset/profile/",
            "locale": session.get("locale", "en"),
        },
    }


@cache_manager.cache.memoize(timeout=60)
def cached_common_bootstrap_data(  # pylint: disable=unused-argument
    user_id: int | None, locale: str
) -> dict[str, Any]:
    """Common data always sent to the client

    The function is memoized as the return value only changes when user permissions
    or configuration values change.
    """

    # should not expose API TOKEN to frontend
    frontend_config = {
        k: (list(conf.get(k)) if isinstance(conf.get(k), set) else conf.get(k))
        for k in FRONTEND_CONF_KEYS
    }

    if conf.get("SLACK_API_TOKEN"):
        frontend_config["ALERT_REPORTS_NOTIFICATION_METHODS"] = [
            ReportRecipientType.EMAIL,
            ReportRecipientType.SLACK,
        ]
    else:
        frontend_config["ALERT_REPORTS_NOTIFICATION_METHODS"] = [
            ReportRecipientType.EMAIL,
        ]

    # verify client has google sheets installed
    available_specs = get_available_engine_specs()
    frontend_config["HAS_GSHEETS_INSTALLED"] = bool(available_specs[GSheetsEngineSpec])

    bootstrap_data = {
        "conf": frontend_config,
        "locale": locale,
        "language_pack": get_language_pack(locale),
        "d3_format": conf.get("D3_FORMAT"),
        "currencies": conf.get("CURRENCIES"),
        "feature_flags": get_feature_flags(),
        "extra_sequential_color_schemes": conf["EXTRA_SEQUENTIAL_COLOR_SCHEMES"],
        "extra_categorical_color_schemes": conf["EXTRA_CATEGORICAL_COLOR_SCHEMES"],
        "theme_overrides": conf["THEME_OVERRIDES"],
        "menu_data": menu_data(g.user),
    }
    bootstrap_data.update(conf["COMMON_BOOTSTRAP_OVERRIDES_FUNC"](bootstrap_data))
    return bootstrap_data


def common_bootstrap_payload() -> dict[str, Any]:
    return {
        **cached_common_bootstrap_data(utils.get_user_id(), get_locale()),
        "flash_messages": get_flashed_messages(with_categories=True),
    }


def get_error_level_from_status_code(  # pylint: disable=invalid-name
    status: int,
) -> ErrorLevel:
    if status < 400:
        return ErrorLevel.INFO
    if status < 500:
        return ErrorLevel.WARNING
    return ErrorLevel.ERROR


# SIP-40 compatible error responses; make sure APIs raise
# SupersetErrorException or SupersetErrorsException
@superset_app.errorhandler(SupersetErrorException)
def show_superset_error(ex: SupersetErrorException) -> FlaskResponse:
    logger.warning("SupersetErrorException", exc_info=True)
    return json_errors_response(errors=[ex.error], status=ex.status)


@superset_app.errorhandler(SupersetErrorsException)
def show_superset_errors(ex: SupersetErrorsException) -> FlaskResponse:
    logger.warning("SupersetErrorsException", exc_info=True)
    return json_errors_response(errors=ex.errors, status=ex.status)


# Redirect to login if the CSRF token is expired
@superset_app.errorhandler(CSRFError)
def refresh_csrf_token(ex: CSRFError) -> FlaskResponse:
    logger.warning("Refresh CSRF token error", exc_info=True)

    if request.is_json:
        return show_http_exception(ex)

    return redirect(appbuilder.get_url_for_login)


@superset_app.errorhandler(HTTPException)
def show_http_exception(ex: HTTPException) -> FlaskResponse:
    logger.warning("HTTPException", exc_info=True)
    if (
        "text/html" in request.accept_mimetypes
        and not config["DEBUG"]
        and ex.code in {404, 500}
    ):
        path = resource_filename("superset", f"static/assets/{ex.code}.html")
        return send_file(path, max_age=0), ex.code

    return json_errors_response(
        errors=[
            SupersetError(
                message=utils.error_msg_from_exception(ex),
                error_type=SupersetErrorType.GENERIC_BACKEND_ERROR,
                level=ErrorLevel.ERROR,
            ),
        ],
        status=ex.code or 500,
    )


# Temporary handler for CommandException; if an API raises a
# CommandException it should be fixed to map it to SupersetErrorException
# or SupersetErrorsException, with a specific status code and error type
@superset_app.errorhandler(CommandException)
def show_command_errors(ex: CommandException) -> FlaskResponse:
    logger.warning("CommandException", exc_info=True)
    if "text/html" in request.accept_mimetypes and not config["DEBUG"]:
        path = resource_filename("superset", "static/assets/500.html")
        return send_file(path, max_age=0), 500

    extra = ex.normalized_messages() if isinstance(ex, CommandInvalidError) else {}
    return json_errors_response(
        errors=[
            SupersetError(
                message=ex.message,
                error_type=SupersetErrorType.GENERIC_COMMAND_ERROR,
                level=get_error_level_from_status_code(ex.status),
                extra=extra,
            ),
        ],
        status=ex.status,
    )


# Catch-all, to ensure all errors from the backend conform to SIP-40
@superset_app.errorhandler(Exception)
def show_unexpected_exception(ex: Exception) -> FlaskResponse:
    logger.exception(ex)
    if "text/html" in request.accept_mimetypes and not config["DEBUG"]:
        path = resource_filename("superset", "static/assets/500.html")
        return send_file(path, max_age=0), 500

    return json_errors_response(
        errors=[
            SupersetError(
                message=utils.error_msg_from_exception(ex),
                error_type=SupersetErrorType.GENERIC_BACKEND_ERROR,
                level=ErrorLevel.ERROR,
            ),
        ],
    )


@superset_app.context_processor
def get_common_bootstrap_data() -> dict[str, Any]:
    def serialize_bootstrap_data() -> str:
        return json.dumps(
            {"common": common_bootstrap_payload()},
            default=utils.pessimistic_json_iso_dttm_ser,
        )

    return {"bootstrap_data": serialize_bootstrap_data}


class SupersetListWidget(ListWidget):  # pylint: disable=too-few-public-methods
    template = "superset/fab_overrides/list.html"


class SupersetModelView(ModelView):
    page_size = 100
    list_widget = SupersetListWidget

    def render_app_template(self) -> FlaskResponse:
        payload = {
            "user": bootstrap_user_data(g.user, include_perms=True),
            "common": common_bootstrap_payload(),
        }
        return self.render_template(
            "superset/spa.html",
            entry="spa",
            bootstrap_data=json.dumps(
                payload, default=utils.pessimistic_json_iso_dttm_ser
            ),
        )


class ListWidgetWithCheckboxes(ListWidget):  # pylint: disable=too-few-public-methods
    """An alternative to list view that renders Boolean fields as checkboxes

    Works in conjunction with the `checkbox` view."""

    template = "superset/fab_overrides/list_with_checkboxes.html"


def validate_json(form: Form, field: Field) -> None:  # pylint: disable=unused-argument
    try:
        json.loads(field.data)
    except Exception as ex:
        logger.exception(ex)
        raise Exception(_("json isn't valid")) from ex


class YamlExportMixin:  # pylint: disable=too-few-public-methods
    """
    Override this if you want a dict response instead, with a certain key.
    Used on DatabaseView for cli compatibility
    """

    yaml_dict_key: str | None = None

    @action("yaml_export", __("Export to YAML"), __("Export to YAML?"), "fa-download")
    def yaml_export(
        self, items: ImportExportMixin | list[ImportExportMixin]
    ) -> FlaskResponse:
        if not isinstance(items, list):
            items = [items]

        data = [t.export_to_dict() for t in items]

        return Response(
            yaml.safe_dump({self.yaml_dict_key: data} if self.yaml_dict_key else data),
            headers=generate_download_headers("yaml"),
            mimetype="application/text",
        )


class DeleteMixin:  # pylint: disable=too-few-public-methods
    def _delete(self: BaseView, primary_key: int) -> None:
        """
        Delete function logic, override to implement different logic
        deletes the record with primary_key = primary_key

        :param primary_key:
            record primary key to delete
        """
        item = self.datamodel.get(primary_key, self._base_filters)
        if not item:
            abort(404)
        try:
            self.pre_delete(item)
        except Exception as ex:  # pylint: disable=broad-except
            flash(str(ex), "danger")
        else:
            view_menu = security_manager.find_view_menu(item.get_perm())
            pvs = (
                security_manager.get_session.query(
                    security_manager.permissionview_model
                )
                .filter_by(view_menu=view_menu)
                .all()
            )

            if self.datamodel.delete(item):
                self.post_delete(item)

                for pv in pvs:
                    security_manager.get_session.delete(pv)

                if view_menu:
                    security_manager.get_session.delete(view_menu)

                security_manager.get_session.commit()

            flash(*self.datamodel.message)
            self.update_redirect()

    @action(
        "muldelete", __("Delete"), __("Delete all Really?"), "fa-trash", single=False
    )
    def muldelete(self: BaseView, items: list[Model]) -> FlaskResponse:
        if not items:
            abort(404)
        for item in items:
            try:
                self.pre_delete(item)
            except Exception as ex:  # pylint: disable=broad-except
                flash(str(ex), "danger")
            else:
                self._delete(item.id)
        self.update_redirect()
        return redirect(self.get_redirect())


class DatasourceFilter(BaseFilter):  # pylint: disable=too-few-public-methods
    def apply(self, query: Query, value: Any) -> Query:
        if security_manager.can_access_all_datasources():
            return query
        query = query.join(
            models.Database,
            models.Database.id == self.model.database_id,
        )
        return query.filter(get_dataset_access_filters(self.model))


class CsvResponse(Response):
    """
    Override Response to take into account csv encoding from config.py
    """

    charset = conf["CSV_EXPORT"].get("encoding", "utf-8")
    default_mimetype = "text/csv"


class XlsxResponse(Response):
    """
    Override Response to use xlsx mimetype
    """

    charset = "utf-8"
    default_mimetype = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def bind_field(
    _: Any, form: DynamicForm, unbound_field: UnboundField, options: dict[Any, Any]
) -> Field:
    """
    Customize how fields are bound by stripping all whitespace.

    :param form: The form
    :param unbound_field: The unbound field
    :param options: The field options
    :returns: The bound field
    """

    filters = unbound_field.kwargs.get("filters", [])
    filters.append(lambda x: x.strip() if isinstance(x, str) else x)
    return unbound_field.bind(form=form, filters=filters, **options)


FlaskForm.Meta.bind_field = bind_field


@superset_app.after_request
def apply_http_headers(response: Response) -> Response:
    """Applies the configuration's http headers to all responses"""

    # HTTP_HEADERS is deprecated, this provides backwards compatibility
    response.headers.extend(
        {**config["OVERRIDE_HTTP_HEADERS"], **config["HTTP_HEADERS"]}
    )

    for k, v in config["DEFAULT_HTTP_HEADERS"].items():
        if k not in response.headers:
            response.headers[k] = v
    return response

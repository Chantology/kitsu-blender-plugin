import logging

from . import client as raw
from . import cache
from . import helpers

_logger = logging.getLogger("gazu")

# events and aio rely on optional dependencies; log the cause instead of
# hiding every ImportError.
try:
    from . import events
except ImportError as exc:
    _logger.debug("gazu.events unavailable: %s", exc)

try:
    from . import aio
except ImportError as exc:
    _logger.debug("gazu.aio unavailable: %s", exc)

from . import asset
from . import casting
from . import context
from . import edit
from . import entity
from . import files
from . import project
from . import project_template
from . import person
from . import scene
from . import search
from . import shot
from . import studio
from . import sync
from . import task
from . import user
from . import playlist
from . import concept

from .exception import (
    AuthFailedException,
    ParameterException,
    NotAuthenticatedException,
)
from .__version__ import __version__


def get_host(client=raw.default_client):
    """
    Return the API host currently configured on the client.
    """
    return raw.get_host(client=client)


def set_host(url, client=raw.default_client):
    """
    Set the API host to query (e.g. "https://kitsu.example.com/api").
    """
    raw.set_host(url, client=client)


def log_in(
    email,
    password,
    totp=None,
    email_otp=None,
    fido_authentication_response=None,
    recovery_code=None,
    client=raw.default_client,
):
    """
    Log in and store the returned tokens on the client for later requests.

    Args:
        email (str): User email.
        password (str): User password.
        totp (str): TOTP code for 2FA.
        email_otp (str): Email OTP code.
        fido_authentication_response: FIDO authentication response.
        recovery_code (str): Recovery code.

    Returns:
        dict: The authentication tokens returned by the API.

    Raises:
        AuthFailedException: when the credentials are rejected.
    """
    tokens = {}
    login_error = None
    try:
        tokens = raw.post(
            "auth/login",
            {
                "email": email,
                "password": password,
                "totp": totp,
                "email_otp": email_otp,
                "fido_authentication_response": fido_authentication_response,
                "recovery_code": recovery_code,
            },
            client=client,
        )
    except (NotAuthenticatedException, ParameterException) as exc:
        # Keep the server's reason (wrong 2FA, locked account, ...) instead
        # of raising a bare AuthFailedException.
        login_error = exc

    if not tokens or tokens.get("login") is False:
        if login_error is not None:
            raise AuthFailedException(str(login_error))
        raise AuthFailedException
    else:
        raw.set_tokens(tokens, client=client)
    return tokens


def send_email_otp(email, client=raw.default_client):
    """
    Ask the API to send a one-time password to the given email.
    """
    return raw.get("auth/email-otp", params={"email": email}, client=client)


def log_out(client=raw.default_client):
    """
    Log out and clear the tokens stored on the client.
    """
    tokens = {}
    try:
        raw.get("auth/logout", client=client)
    except (ParameterException, NotAuthenticatedException):
        # Clear the local tokens even if the server rejects the logout
        # (e.g. the access token has already expired).
        pass
    raw.set_tokens(tokens, client=client)
    return tokens


def refresh_access_token(client=raw.default_client):
    """
    Refresh the access token using the stored refresh token.
    """
    return client.refresh_access_token()


def get_event_host(client=raw.default_client):
    """
    Return the event (websocket) host configured on the client.
    """
    return raw.get_event_host(client=client)


def set_event_host(url, client=raw.default_client):
    """
    Set the event (websocket) host used to listen to Kitsu events.
    """
    raw.set_event_host(url, client=client)


def create_session(
    host,
    email,
    password,
    totp=None,
    email_otp=None,
    fido_authentication_response=None,
    recovery_code=None,
    ssl_verify=True,
    cert=None,
    use_refresh_token=False,
    callback_not_authenticated=None,
):
    """
    Create a logged-in KitsuClient for use as a context manager.

    Usage::

        with gazu.create_session(host, email, password) as client:
            assets = gazu.asset.all_assets(client=client)
        # auto logout + session close

    Args:
        host (str): The host URL (e.g. "https://kitsu.example.com/api").
        email (str): User email.
        password (str): User password.
        totp (str): TOTP code for 2FA.
        email_otp (str): Email OTP code.
        fido_authentication_response: FIDO authentication response.
        recovery_code (str): Recovery code.
        ssl_verify (bool): Whether to verify SSL certificates.
        cert (str): Path to a client certificate.
        use_refresh_token (bool): Whether to automatically refresh tokens.
        callback_not_authenticated (function): Callback when not authenticated.

    Returns:
        KitsuClient: A logged-in client usable as a context manager.
    """
    client = raw.create_client(
        host,
        ssl_verify=ssl_verify,
        cert=cert,
        use_refresh_token=use_refresh_token,
        callback_not_authenticated=callback_not_authenticated,
    )
    try:
        log_in(
            email,
            password,
            totp=totp,
            email_otp=email_otp,
            fido_authentication_response=fido_authentication_response,
            recovery_code=recovery_code,
            client=client,
        )
    except Exception:
        # Don't leak the underlying requests session if login fails.
        client.session.close()
        raise
    return client


def set_token(token, client=raw.default_client):
    """
    Store authentication token to reuse them for all requests.

    Args:
        token (dict / str): Tokens to use for authentication.
    """

    if isinstance(token, dict):
        return raw.set_tokens(token, client=client)
    else:
        return raw.set_tokens({"access_token": token}, client=client)

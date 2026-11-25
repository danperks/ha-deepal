"""Plain-English meanings for vehicle command result codes.

The four-digit and three-digit tables are the official Deepal app's own English
strings (v1.12.0). ``1032`` is the decimal result the car returns when it is
powered on and refuses a remote command. These wordings are a fallback for when
the vehicle does not send its own message. Callers always keep the numeric code,
and a non-empty vehicle message wins over this table.

Omitted on purpose: generic "Operation failed" entries, codes that mean success
or progress, and account, login, QR, and sharing errors.
"""

from __future__ import annotations

RKE_RESULT_MESSAGES: dict[str, str] = {
    "1032": "The car is powered on",
    "3001": "Too many attempts; please try again later",
    "3004": "Execution timeout; please try again later",
    "3006": "Previous Command is executing; please try again later",
    "3007": "Vehicle not powered off",
    "3008": "Vehicle not powered on",
    "3009": "Left Front Door not closed; unable to lock",
    "300a": "Right Front Door not closed; unable to lock",
    "300b": "Left Rear Door not closed; unable to lock",
    "300c": "Right Rear Door not closed; unable to lock",
    "300d": "Tailgate not closed; unable to lock",
    "300e": "Vehicle does not support this feature",
    "3011": "Cannot unlock vehicle when moving.",
    "3012": "Previous Command is executing; please try again later",
    "3016": "Operation failed; please close the Bonnet and try again.",
    "3017": "Please close the Partition Glass.",
    "4001": "Remote Control unavailable when vehicle in use.",
    "4002": "Cannot unlock vehicle when moving.",
    "4003": "Cannot unlock vehicle when moving.",
    "4004": "Remote Control unavailable when vehicle in use.",
    "4005": "Low Battery Power",
    "4006": "Operation failed; OTA Status not met.",
    "4007": "Operation failed; incorrect Door Status.",
    "4008": "Operation failed; incorrect Door Lock Status.",
    "4009": "Anti-Pinch triggered; please remove obstruction and retry",
    "400a": "Motor overheating, please retry later",
    "400b": "Anti-Misuse Protection triggered.",
    "400d": "Please complete Initialisation first.",
    "4010": "Execution timeout; please try again later",
    "4012": "Anti-Pinch triggered; please remove obstruction and retry",
    "4013": "Anti-Pinch Abuse Protection Mode active.",
    "4014": "Anti-Collision Abuse Protection Mode active.",
    "4016": "Operation failed, rear door glass status not met",
    "4017": "Operation failed; Rear Dome status incorrect.",
    "401e": "Trunk temporarily unavailable in trailer mode.",
    "5002": "Front Wiper Maintenance Protection triggered.",
    "5003": "Resume operation after interruption.",
}

CLOUD_ERROR_MESSAGES: dict[str, str] = {
    "010": "Vehicle in Maintenance Mode; Remote Control is temporarily unavailable.",
    "011": "The session has expired; please start again.",
    "012": "Operation failed; Vehicle Network fault",
    "019": "Vehicle Backup Schedule conflict.",
    "020": "Limit for number of Vehicle Backup Plans reached.",
    "021": "Vehicle Backup Plan does not exist.",
    "039": "Controller is not responding; please try again later.",
    "040": "Current Vehicle does not support this command",
    "041": "Vehicle performing other Operations; please try again later.",
    "042": "Vehicle in operation; please try again later",
    "043": "Operation timed out; please try again later.",
    "044": "Controller response timeout; please try again later.",
    "045": "Too many Remote Attempts; please start the vehicle manually before continuing.",
    "046": "Vehicle unable to start; please contact Customer Service.",
    "047": "Vehicle Status not satisfactory; please contact Customer Service.",
    "048": "Operation failed; please close the Door first",
    "049": "Operation failed; please close the Tailgate first.",
    "050": "Operation failed; Power is not OFF",
    "051": "Operation failed; Engine Status does not support Remote Start.",
    "052": "Operation failed; Traction Battery low.",
    "053": "Operation failed; one of the Doors, Bonnet or Tailgate is not closed.",
    "055": "Operation failed; Vehicle Immobiliser is activated. Please go to the Vehicle Terminal to check.",
    "056": "Hazard Lights are on",
    "058": "Vehicle is not stationary.",
    "059": "Operation failed; Engine is running",
    "060": "Operation failed, Electronic Parking Brake is released",
    "061": "Fuel Level low; Remote Start disabled.",
    "062": "Startup System failure; Remote Start disabled.",
    "063": "Engine Oil Pressure too low; to avoid affecting normal starting, Remote Start has been disabled.",
    "064": "Engine Coolant Temperature too high; to avoid affecting normal startup, Remote Start is disabled.",
    "065": "EFI System fault; to avoid affecting normal startup, Remote Start is disabled.",
    "066": "Engine Run Time expired; normal Engine Shutdown.",
    "067": "External Temperature too low; Remote Start disabled.",
    "068": "Perimeter Anti-Theft is not armed, please make sure the Doors, Bonnet and Tailgate are closed and locked.",
    "069": "Operation failed; vehicle is not in Neutral (N).",
    "072": "Battery Power too low; Remote Air Conditioning has been exited.",
    "073": "Operation failed; Window Anti-Pinch triggered.",
    "074": "Operation failed; Sunroof Anti-Pinch triggered.",
    "075": "Operation failed, unsatisfactory Temperature",
    "076": "Operation failed; Sunshade not initialised.",
    "077": "Operation failed; please initialise Window manually.",
    "078": "Remote Start Time has reached the upper limit",
    "079": "Operation failed; Tailgate Anti-Pinch triggered.",
    "080": "Tailgate operating",
    "081": "No Charging Gun connected.",
    "082": "System error; Function cannot be turned on or off.",
    "083": "Vehicle not in PARK; exiting function.",
    "085": "Cabin Temperature Control in progress; please exit and try again.",
    "086": "Low Battery, unable to start.",
    "087": "Windows are not closed, unable to start.",
    "088": "Operation failed; Door or Tailgate is not closed",
    "089": "Operation failed; vehicle is not in PARK (P).",
    "091": "Seat Ventilation Operation failed",
    "092": "Seat Heating Operation failed",
    "097": "Vehicle upgrade status not met",
}


def describe_vehicle_code(code: object) -> str | None:
    """Return the app's wording for a result code, or None when it is unknown.

    Four-digit codes match ``"3006"``, ``"401E"``, and ``1032``. Zero-padded
    cloud codes match ``"039"``. A bare integer such as ``39`` does not match
    the cloud table.
    """
    text = str(code).strip().lower() if code is not None else ""
    if len(text) == 4 and text in RKE_RESULT_MESSAGES:
        return RKE_RESULT_MESSAGES[text]
    if len(text) == 3 and text in CLOUD_ERROR_MESSAGES:
        return CLOUD_ERROR_MESSAGES[text]
    return None


def command_result_detail(code: object, vehicle_message: object = None) -> str | None:
    """Prefer a non-empty vehicle message, otherwise the official-app wording."""
    if vehicle_message is not None:
        text = str(vehicle_message).strip()
        if text:
            return text
    return describe_vehicle_code(code)


def command_failure_message(result_code: object, error_message: object = None) -> str:
    """Build the REST command error. The numeric code is always included."""
    detail = command_result_detail(result_code, error_message)
    if detail:
        return f"Deepal command failed with result code {result_code}: {detail}"
    return f"Deepal command failed with result code {result_code}"

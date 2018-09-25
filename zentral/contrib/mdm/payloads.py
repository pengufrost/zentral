import logging
import plistlib
from django.http import HttpResponse
from django.urls import reverse
from zentral.conf import settings
from zentral.utils.certificates import split_certificate_chain
from zentral.utils.payloads import generate_payload_uuid, get_payload_identifier
from zentral.utils.payloads import sign_payload_openssl


logger = logging.getLogger("zentral.contrib.mdm.payloads")


def build_configuration_profile_response(data, filename):
    response = HttpResponse(data, content_type="application/x-apple-aspen-config")
    response['Content-Disposition'] = 'attachment; filename="{}.mobileconfig"'.format(filename)
    return response


def build_profile(display_name, suffix, content,
                  payload_type="Configuration", payload_description=None,
                  sign=True, encrypt=False):
    profile = {"PayloadUUID": generate_payload_uuid(),
               "PayloadIdentifier": get_payload_identifier(suffix),
               "PayloadVersion": 1,
               "PayloadDisplayName": display_name,
               "PayloadType": payload_type,  # Only known exception: "Profile Service"
               "PayloadContent": content}
    if payload_description:
        profile["PayloadDescription"] = payload_description
    data = plistlib.dumps(profile)
    if sign:
        data = sign_payload_openssl(data)
    return data


def build_payload(payload_type, payload_display_name, suffix, content, payload_version=1, encapsulate_content=False):
    payload = {"PayloadUUID": generate_payload_uuid(),
               "PayloadType": payload_type,
               "PayloadDisplayName": payload_display_name,
               "PayloadIdentifier": get_payload_identifier(suffix),
               "PayloadVersion": payload_version}
    if encapsulate_content:
        # for scep, certificates TODO: what else ?
        payload["PayloadContent"] = content
    else:
        payload.update(content)
    return payload


def build_root_ca_payloads():
    root_certificates = []
    payloads = []
    for api_settings_attr, name, suffix in (("tls_server_certs",
                                             "Zentral - root CA",
                                             "tls-root-ca-cert"),
                                            ("tls_server_certs_client_certificate_authenticated",
                                             "Zentral client certificate authenticated - root CA",
                                             "tls-clicertauth-root-ca")):
        if api_settings_attr not in settings["api"]:
            logger.warning("Missing %s key in api settings", api_settings_attr)
            continue
        certificate_chain_filename = settings["api"][api_settings_attr]
        root_certificate = split_certificate_chain(certificate_chain_filename)[-1]
        if root_certificate not in root_certificates:
            payloads.append(build_payload("com.apple.security.pem",
                                          name, suffix,
                                          root_certificate.encode("utf-8"),
                                          encapsulate_content=True))
    return payloads


def build_root_ca_configuration_profile():
    return build_profile("Zentral - root CA certificates",
                         "root-ca-certificates",
                         build_root_ca_payloads())


def build_scep_payload(enrollment_session):
    return build_payload("com.apple.security.scep",
                         enrollment_session.get_payload_name(),
                         "scep",
                         {"URL": "{}/scep".format(settings["api"]["tls_hostname"]),
                          "Subject": [[["CN", enrollment_session.get_common_name()]],
                                      [["2.5.4.5", enrollment_session.get_serial_number()]],
                                      [["O", enrollment_session.get_organization()]]],
                          "Challenge": enrollment_session.get_challenge(),
                          "Keysize": 2048,
                          "KeyType": "RSA",
                          "KeyUsage": 5,  # 1 is signing, 4 is encryption, 5 is both signing and encryption
                          },
                         encapsulate_content=True)


def build_profile_service_configuration_profile(ota_enrollment):
    return build_profile("Zentral - OTA MDM Enrollment",
                         "profile-service",
                         {"URL": "{}{}".format(settings["api"]["tls_hostname"],
                                               reverse("mdm:ota_enroll")),
                          "DeviceAttributes": ["UDID",
                                               "VERSION",
                                               "PRODUCT",
                                               "SERIAL",
                                               "MEID",
                                               "IMEI"],
                          "Challenge": ota_enrollment.enrollment_secret.secret},
                         payload_type="Profile Service",
                         payload_description="Install this profile to enroll your device with Zentral")


def build_ota_scep_configuration_profile(ota_enrollment_session):
    return build_profile(ota_enrollment_session.get_payload_name(), "scep",
                         [build_scep_payload(ota_enrollment_session)])


def build_mdm_configuration_profile(enrollment_session, push_certificate):
    scep_payload = build_scep_payload(enrollment_session)
    payloads = build_root_ca_payloads()
    payloads.extend([
        scep_payload,
        build_payload("com.apple.mdm",
                      "Zentral - MDM",
                      "mdm",
                      {"IdentityCertificateUUID": scep_payload["PayloadUUID"],
                       "Topic": push_certificate.topic,
                       "ServerURL": "{}{}".format(
                           settings["api"]["tls_hostname_client_certificate_authenticated"],
                           reverse("mdm:connect")),
                       "ServerCapabilities": ["com.apple.mdm.per-user-connections"],
                       "CheckInURL": "{}{}".format(
                           settings["api"]["tls_hostname_client_certificate_authenticated"],
                           reverse("mdm:checkin")),
                       "CheckOutWhenRemoved": True,
                       "AccessRights": 8191,  # TODO: config
                       })
    ])
    return build_profile("Zentral - MDM enrollment", "mdm", payloads)

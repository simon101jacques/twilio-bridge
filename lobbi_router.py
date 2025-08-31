# # lobbi_router.py
# import logging
# import re
# import firebase_admin
# from firebase_admin import auth as fb_auth, firestore as fb_fs

# log = logging.getLogger("bridge")

# # Init Firebase once
# if not firebase_admin._apps:
#     firebase_admin.initialize_app()

# db: fb_fs.Client = fb_fs.client()


# def lang_for_caller(e164_from: str | None) -> str:
#     """Return 'it-IT' for +39, else 'en-US'."""
#     if e164_from and e164_from.strip().startswith("+39"):
#         return "it-IT"
#     return "en-US"


# def intro_texts(lang: str) -> tuple[str, str]:
#     """Localized intro/ready messages."""
#     if lang.startswith("it"):
#         return (
#             "Benvenuto in Lobbi del tuo condominio. Sto verificando l'accesso.",
#             "Quando sei pronto, puoi iniziare a parlare."
#         )
#     return (
#         "Welcome to your building Lobbi. Checking access.",
#         "Okay, you can start talking."
#     )


# def resolve_building_for_phone(e164_from: str | None) -> tuple[int | None, str | None]:
#     """
#     Resolve numeric building_id for caller phone:
#       Auth phone -> users/{uid} -> first numeric in buildingID[]
#     Returns: (building_id, uid)
#     """
#     try:
#         if not e164_from:
#             return (None, None)

#         user = fb_auth.get_user_by_phone_number(e164_from)
#         uid = user.uid

#         udoc = db.collection("users").document(uid).get()
#         building_id = None

#         if udoc.exists:
#             data = udoc.to_dict() or {}
#             building_id = _first_numeric_value(data.get("buildingID"))

#         return (building_id, uid)
#     except fb_auth.UserNotFoundError:
#         return (None, None)
#     except Exception as e:
#         log.warning(f"[router] resolve_building_for_phone error: {e}")
#         return (None, None)


# def query_building_collection(building_id: int, collection: str) -> list[dict]:
#     """
#     Query votes or maintenance collection for a given building_id.
#     Returns list of dicts (Firestore docs).
#     """
#     if not building_id:
#         return []

#     try:
#         coll_ref = db.collection(collection)
#         docs = coll_ref.where("buildingID", "==", building_id).stream()
#         results = []
#         for d in docs:
#             item = d.to_dict()
#             item["id"] = d.id
#             results.append(item)
#         return results
#     except Exception as e:
#         log.warning(f"[router] query_building_collection error: {e}")
#         return []


# # ---- internal ----
# def _first_numeric_value(arr) -> int | None:
#     if not isinstance(arr, (list, tuple)):
#         return None
#     for v in arr:
#         if isinstance(v, int):
#             return v
#         if isinstance(v, str) and re.fullmatch(r"\d+", v or ""):
#             try:
#                 return int(v)
#             except Exception:
#                 continue
#     return None

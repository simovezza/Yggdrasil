"""Urology views."""

import json as _json
import logging
import os
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponseGone, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from common import export_catalog, export_ui
from common.deletion import FolderNotEmpty, delete_folder as _delete_folder
from common.export_processing import (
    ExportProcessor,
    build_shared_download_url as _build_shared_download_url,
    format_file_size,
    kill_export_processes as _kill_export_processes,
    recover_stuck_export as _recover_stuck_export,
    start_export_processing,
)
from common.export_share import is_share_expired, resolve_share_expiry
from common.file_access import exists as artifact_exists, streaming_response
from common.modality_config import (
    modality_status,
    rerun_step_labels,
    rerunnable_steps_for_patient,
)
from common.models import FileRegistry, Modality, Project, ProjectAccess
from common.object_storage import get_object_storage
from common.permissions import (
    filter_folders_for_user,
    filter_patients_for_user,
    user_can_delete_single_patient,
    user_can_write_patient_annotations,
    user_has_project_access,
    user_is_project_admin,
)
from common.project_filters import presence_filter_specs

from .export_config import install_urology_export_mappings
from .forms import PatientForm, PatientManagementForm
from .helpers import redirect_with_namespace, render_with_fallback
from .models import Export, Folder, Patient, Tag

logger = logging.getLogger(__name__)


def home(request):
    return redirect("urology:patient_list")


@login_required
def select_project(request, project_id):
    project = get_object_or_404(Project, id=project_id, is_active=True)
    if not user_is_project_admin(request.user, project):
        has_access = ProjectAccess.objects.filter(
            user=request.user, project=project
        ).exists()
        if not has_access:
            messages.error(
                request, f"You don't have access to the {project.name} project."
            )
            return redirect("home")
    request.session["current_project_id"] = project.id
    return redirect("urology:patient_list")


@login_required
def patient_list(request):
    patients = (
        Patient.objects.select_related("dataset", "uploaded_by")
        .prefetch_related(
            "voice_captions",
            "voice_captions__user",
            "tags",
            "modalities",
            "files",
            "files__modality",
            "jobs",
        )
    )
    current_project_id = request.session.get("current_project_id")
    if current_project_id and any(field.name == "project" for field in Patient._meta.fields):
        patients = patients.filter(project_id=current_project_id)
    patients = filter_patients_for_user(request.user, patients, "urology")
    patients_for_folder_counts = patients

    search_query = request.GET.get("search", "").strip()
    if search_query:
        patients = patients.filter(
            Q(name__icontains=search_query) | Q(patient_id__icontains=search_query)
        )

    folder_id = request.GET.get("folder")
    if folder_id and folder_id != "all":
        if folder_id == "root":
            patients = patients.filter(folder__isnull=True)
        else:
            try:
                patients = patients.filter(folder_id=int(folder_id))
            except ValueError:
                pass

    tags_selected = request.GET.getlist("tags")
    if tags_selected:
        patients = patients.filter(tags__name__in=tags_selected).distinct()

    has_reports_filter = request.GET.get("has_reports", "")
    if has_reports_filter == "yes":
        patients = patients.filter(voice_captions__isnull=False).distinct()

    patients = patients.order_by("-uploaded_at")
    allowed_modalities = []
    current_project = None
    if current_project_id:
        project = (
            Project.objects.filter(id=current_project_id)
            .prefetch_related("modalities", "annotation_methods")
            .first()
        )
        if project:
            current_project = project
            allowed_modalities = list(project.modalities.filter(is_active=True))

    if not allowed_modalities:
        # Fall back to default Urology modalities
        allowed_modalities = list(
            Modality.objects.filter(domain="urology", is_active=True).order_by("name")
        )

    status_filters = {}
    for modality in allowed_modalities:
        slug = modality.slug or ""
        if slug and slug != "rawzip":
            value = request.GET.get(f"status_{slug}", "").strip()
            if value in {"processed", "processing", "failed"}:
                status_filters[slug] = value

    patients_with_status = []
    is_admin = user_is_project_admin(request.user, request)
    for patient in patients:
        voice_captions = list(patient.voice_captions.all())
        patient_files = list(patient.files.all())
        patient_jobs = list(patient.jobs.all()) if hasattr(patient, "jobs") else []
        files_by_modality = {}
        for file_obj in patient_files:
            if file_obj.modality and file_obj.modality.slug:
                files_by_modality.setdefault(file_obj.modality.slug, []).append(file_obj)
        jobs_by_modality = {}
        for job in patient_jobs:
            jobs_by_modality.setdefault(job.modality_slug, []).append(job)
        modality_status_list = []
        for modality in allowed_modalities:
            slug = modality.slug or ""
            if slug in {"rawzip", "voice"}:
                continue
            status = modality_status(
                slug,
                jobs_by_modality.get(slug, []),
                bool(files_by_modality.get(slug)),
            )
            modality_status_list.append({
                "slug": slug,
                "name": modality.name,
                "icon": modality.icon or "",
                "label": modality.label or "",
                "status": status,
            })
        patients_with_status.append({
            "patient": patient,
            "voice_caption_processing": any(
                vc.processing_status in ["pending", "processing"] for vc in voice_captions
            ),
            "voice_caption_processed": bool(voice_captions)
            and all(vc.processing_status == "completed" for vc in voice_captions),
            "voice_caption_count": len(voice_captions),
            "voice_annotators": list({vc.user.username for vc in voice_captions}),
            "tags": patient.tag_names(),
            "folder": patient.folder,
            "available_modalities": [m.slug for m in patient.modalities.all()],
            "modality_statuses": {item["slug"]: item["status"] for item in modality_status_list},
            "modality_status_list": modality_status_list,
            "rerunnable_steps": rerunnable_steps_for_patient(
                patient_files, modality_status_list, patient=patient
            ),
            "can_delete": bool(
                is_admin
                or (
                    patient.folder
                    and user_can_delete_single_patient(
                        request.user, patient.folder, patient.project
                    )
                )
            ),
        })

    if status_filters:
        patients_with_status = [
            item
            for item in patients_with_status
            if all(
                item["modality_statuses"].get(slug, "absent") == value
                for slug, value in status_filters.items()
            )
        ]

    try:
        per_page = int(request.GET.get("per_page", 10))
    except (TypeError, ValueError):
        per_page = 10
    if per_page not in {10, 20, 50, 100}:
        per_page = 10
    page_obj = Paginator(patients_with_status, per_page).get_page(request.GET.get("page"))
    project_id = current_project_id
    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(parent__isnull=True)
        .filter(project_id=project_id if project_id else None)
        .order_by("name"),
        "urology",
    )
    projects_for_sidebar = Project.objects.filter(domain="urology", is_active=True)
    if not request.user.is_staff:
        accessible_project_ids = ProjectAccess.objects.filter(
            user=request.user
        ).values_list("project_id", flat=True)
        projects_for_sidebar = projects_for_sidebar.filter(id__in=accessible_project_ids)

    context = {
        "page_obj": page_obj,
        "current_project_id": current_project_id,
        "projects": projects_for_sidebar.order_by("name"),
        "search_query": search_query,
        "folder_id": folder_id or "all",
        "selected_tags": tags_selected,
        "folders": [
            {
                "folder": folder,
                "patient_count": patients_for_folder_counts.filter(folder=folder).count(),
            }
            for folder in folders
        ],
        "all_tags": Tag.objects.all().order_by("name"),
        "per_page": per_page,
        "user_profile": request.user.profile,
        "is_admin_user": is_admin,
        "has_reports_filter": has_reports_filter,
        "presence_filter_specs": presence_filter_specs(
            request, current_project, {m.slug for m in allowed_modalities}
        ),
        "allowed_modalities": allowed_modalities,
        "status_filters": status_filters,
        "modality_filter_specs": [
            {
                "slug": m.slug,
                "name": m.name,
                "icon": m.icon or "",
                "label": m.label or "",
                "value": status_filters.get(m.slug, ""),
            }
            for m in allowed_modalities
            if m.slug != "rawzip"
        ],
        "rerun_step_labels": rerun_step_labels(
            None,
            [
                {"slug": m.slug, "name": m.name, "label": m.label or "", "status": None}
                for m in allowed_modalities
                if m.slug != "rawzip"
            ],
        ),
    }
    return render_with_fallback(request, "patient_list", context)


@login_required
def upload_patient(request):
    """Clean placeholder for the upcoming Urology upload form."""
    return render(
        request,
        "urology/placeholder.html",
        {
            "title": "Upload Scan",
            "subtitle": "Urology Scans Upload Pipeline",
            "page_icon": "upload",
            "message": "The Urology scan upload pipeline (mpMRI & WSI digital pathology) is currently under active development.",
        },
    )


@login_required
def patient_detail(request, patient_id):
    """Clean placeholder for the upcoming Urology patient multimodal viewer."""
    patient = get_object_or_404(Patient, patient_id=patient_id)
    return render(
        request,
        "urology/placeholder.html",
        {
            "title": f"Patient {patient.patient_id} - {patient.name}",
            "subtitle": "Urology Multimodal Viewer",
            "page_icon": "microscope",
            "message": "The Urology multimodal viewer (mpMRI NIfTI & WSI digital pathology slides) is currently under active development.",
        },
    )


@login_required
@require_POST
def update_patient_name(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not (
        user_is_project_admin(request.user, patient.project)
        or user_can_write_patient_annotations(request.user, patient)
    ):
        return JsonResponse({"error": "Permission denied"}, status=403)
    try:
        data = _json.loads(request.body)
    except _json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    name = (data.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "Name cannot be empty"}, status=400)
    patient.name = name[:100]
    patient.save(update_fields=["name"])
    return JsonResponse({"success": True, "name": patient.name})


@login_required
@require_POST
def delete_patient(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    can_delete = bool(
        user_is_project_admin(request.user, patient.project)
        or (
            patient.folder
            and user_can_delete_single_patient(
                request.user, patient.folder, patient.project
            )
        )
    )
    if not can_delete:
        return JsonResponse(
            {
                "success": False,
                "error": "You do not have permission to delete this patient.",
            },
            status=403,
        )
    patient.deleted = True
    patient.save(update_fields=["deleted"])
    return JsonResponse({"success": True, "message": "Scan deleted successfully"})


@login_required
@require_POST
def bulk_delete_patients(request):
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )

    scan_ids = data.get("scan_ids", [])
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse(
            {"success": False, "error": "scan_ids list is required"}, status=400
        )

    if not user_is_project_admin(request.user, "urology"):
        return JsonResponse(
            {
                "success": False,
                "error": "You do not have permission to bulk delete scans.",
            },
            status=403,
        )

    deleted_count = Patient.objects.filter(patient_id__in=scan_ids).update(deleted=True)
    if not deleted_count:
        return JsonResponse(
            {"success": False, "error": "No valid scans found to delete"}, status=404
        )

    return JsonResponse(
        {
            "success": True,
            "message": f"Successfully deleted {deleted_count} scans.",
            "deleted_count": deleted_count,
        }
    )


@login_required
@require_POST
def bulk_purge_patients(request):
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )

    scan_ids = data.get("scan_ids", [])
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse(
            {"success": False, "error": "scan_ids list is required"}, status=400
        )

    if not user_is_project_admin(request.user, "urology"):
        return JsonResponse(
            {
                "success": False,
                "error": "You do not have permission to permanently delete scans.",
            },
            status=403,
        )

    patients = Patient.objects.filter(patient_id__in=scan_ids)
    found_ids = list(patients.values_list("patient_id", flat=True))
    if not found_ids:
        return JsonResponse(
            {"success": False, "error": "No valid scans found to delete"}, status=404
        )

    storage = get_object_storage()
    file_paths = list(
        FileRegistry.objects.filter(urology_patient_id__in=found_ids).values_list(
            "file_path", flat=True
        )
    )
    storage_errors = []
    for file_path in file_paths:
        try:
            storage.delete(file_path)
        except Exception as exc:
            logger.exception("Error deleting object storage file %s", file_path)
            storage_errors.append(file_path)

    deleted_count, _ = patients.delete()

    response = {
        "success": True,
        "message": f"Permanently deleted {len(found_ids)} scan(s) and {len(file_paths)} file(s).",
        "deleted_count": len(found_ids),
        "files_deleted": len(file_paths) - len(storage_errors),
    }
    if storage_errors:
        response["storage_errors"] = storage_errors
    return JsonResponse(response)


@login_required
@require_POST
def rerun_processing(request, patient_id):
    return JsonResponse(
        {"success": False, "error": "Urology processing rerun is not configured."},
        status=400,
    )


@login_required
@require_POST
def bulk_rerun_processing(request):
    return JsonResponse(
        {"success": False, "error": "Urology bulk processing rerun is not configured."},
        status=400,
    )


@login_required
def user_profile(request, username=None):
    return render(request, "urology/profile.html", {"profile_user": request.user})


@login_required
@require_POST
def create_folder(request):
    try:
        if not user_is_project_admin(request.user, "urology"):
            return JsonResponse({"error": "Permission denied"}, status=403)

        data = _json.loads(request.body) if request.body else request.POST
        name = (data.get("name") or "").strip()
        if not name:
            return JsonResponse({"error": "Folder name is required"}, status=400)

        current_project_id = request.session.get("current_project_id")
        project = None
        if current_project_id:
            project = Project.objects.filter(
                id=current_project_id, domain="urology"
            ).first()
        if not project:
            project = Project.objects.filter(domain="urology").first()

        folder, created = Folder.objects.get_or_create(
            name=name,
            project=project,
            parent=None,
            defaults={"created_by": request.user},
        )
        return JsonResponse(
            {
                "success": True,
                "folder": {
                    "id": folder.id,
                    "name": folder.name,
                    "path": folder.name,
                    "created": created,
                },
            }
        )
    except Exception as exc:
        logger.exception("Error creating urology folder")
        return JsonResponse({"error": str(exc)}, status=500)


@login_required
def folder_stats(request, folder_id):
    folder = get_object_or_404(Folder, id=folder_id)
    return JsonResponse(
        {
            "success": True,
            "folder": {"id": folder.id, "name": folder.name},
            "stats": {"patient_count": folder.patients.count()},
        }
    )


@login_required
@require_POST
def rename_folder(request, folder_id):
    if not user_is_project_admin(request.user, "urology"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )
    name = (data.get("name") or "").strip()
    if not name:
        return JsonResponse(
            {"success": False, "error": "Folder name is required"}, status=400
        )
    folder = get_object_or_404(Folder, id=folder_id)
    folder.name = name
    folder.parent = None
    folder.save(update_fields=["name", "parent"])
    return JsonResponse(
        {"success": True, "folder": {"id": folder.id, "name": folder.name}}
    )


@login_required
@require_http_methods(["DELETE"])
def delete_folder(request, folder_id):
    folder = get_object_or_404(Folder, id=folder_id)
    if not user_is_project_admin(request.user, folder.project):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    try:
        unfiled = _delete_folder(folder, force=request.GET.get("force") == "true")
    except FolderNotEmpty as exc:
        return JsonResponse(
            {"success": False, "error": str(exc), "patient_count": exc.patient_count},
            status=400,
        )
    return JsonResponse({"success": True, "unfiled_patients": unfiled})


@login_required
@require_POST
def move_patients_to_folder(request):
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )
    scan_ids = data.get("scan_ids", [])
    folder_id = data.get("folder_id")
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse(
            {"success": False, "error": "scan_ids list is required"}, status=400
        )
    if not user_is_project_admin(request.user, "urology"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    folder = None
    if folder_id and folder_id not in ("root", "all"):
        folder = get_object_or_404(Folder, id=folder_id)
    patients = Patient.objects.filter(patient_id__in=scan_ids)
    updated = 0
    for patient in patients:
        patient.folder = folder
        if folder:
            patient.project = folder.project
        patient.save(update_fields=["folder", "project"])
        updated += 1
    return JsonResponse({"success": True, "updated": updated})


@login_required
@require_POST
def add_patients_to_folder(request):
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )
    scan_ids = data.get("scan_ids", [])
    folder_id = data.get("folder_id")
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse(
            {"success": False, "error": "scan_ids list is required"}, status=400
        )
    if not folder_id or folder_id in ("root", "all"):
        return JsonResponse(
            {"success": False, "error": "A specific folder_id is required"}, status=400
        )
    if not user_is_project_admin(request.user, "urology"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    folder = get_object_or_404(Folder, id=folder_id)
    patients = Patient.objects.filter(patient_id__in=scan_ids)
    updated = 0
    for patient in patients:
        patient.folder = folder
        patient.project = folder.project
        patient.save(update_fields=["folder", "project"])
        updated += 1
    return JsonResponse({"success": True, "updated": updated})


@login_required
@require_POST
def remove_patients_from_folder(request):
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )
    scan_ids = data.get("scan_ids", [])
    folder_id = data.get("folder_id")
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse(
            {"success": False, "error": "scan_ids list is required"}, status=400
        )
    if not folder_id or folder_id in ("root", "all"):
        return JsonResponse(
            {"success": False, "error": "A specific folder_id is required"}, status=400
        )
    if not user_is_project_admin(request.user, "urology"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    folder = get_object_or_404(Folder, id=folder_id)
    patients = Patient.objects.filter(patient_id__in=scan_ids)
    updated = 0
    for patient in patients:
        if patient.folder_id != folder.id:
            continue
        default_folder = Folder.objects.filter(
            project=patient.project, parent__isnull=True
        ).first()
        patient.folder = default_folder or folder
        patient.save(update_fields=["folder"])
        updated += 1
    return JsonResponse({"success": True, "updated": updated})


@login_required
@require_POST
def add_patient_tag(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not (
        user_is_project_admin(request.user, patient.project)
        or user_can_write_patient_annotations(request.user, patient)
    ):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )
    tag_name = (data.get("tag") or data.get("name") or "").strip()
    if not tag_name:
        return JsonResponse(
            {"success": False, "error": "Tag name required"}, status=400
        )
    tag, _ = Tag.objects.get_or_create(name=tag_name)
    patient.tags.add(tag)
    return JsonResponse({"success": True, "tags": patient.tag_names()})


@login_required
@require_POST
def remove_patient_tag(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not (
        user_is_project_admin(request.user, patient.project)
        or user_can_write_patient_annotations(request.user, patient)
    ):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )
    tag_name = (data.get("tag") or data.get("name") or "").strip()
    if not tag_name:
        return JsonResponse(
            {"success": False, "error": "Tag name required"}, status=400
        )
    tag = Tag.objects.filter(name=tag_name).first()
    if not tag:
        return JsonResponse({"success": False, "error": "Tag not found"}, status=404)
    patient.tags.remove(tag)
    return JsonResponse({"success": True, "tags": patient.tag_names()})


def _with_urology_export_mappings(view_func):
    def wrapped(request, *args, **kwargs):
        install_urology_export_mappings()
        return view_func(request, *args, **kwargs)

    return wrapped


def _urology_shared_export_availability(share_token):
    export = Export.objects.filter(share_token=share_token).first()
    if not export:
        return None, False, "invalid"
    if export.share_mode == "private":
        return export, False, "private"
    if is_share_expired(export):
        return export, False, "expired"
    if export.status != "completed":
        return export, False, "not_completed"
    if not export.file_path or not artifact_exists(export.file_path):
        return export, False, "missing_file"
    return export, True, ""


def _current_export_project(request):
    project_id = request.session.get("current_project_id")
    if not project_id:
        return None
    return (
        Project.objects.filter(id=project_id)
        .prefetch_related("modalities", "annotation_methods", "disabled_steps")
        .first()
    )


@login_required
@_with_urology_export_mappings
def export_list(request):
    exports = Export.objects.filter(user=request.user).order_by("-created_at")

    exports_with_sizes = [
        {
            "export": export,
            "size_display": format_file_size(export.file_size)
            if export.file_size
            else None,
        }
        for export in exports
    ]

    paginator = Paginator(exports_with_sizes, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "maxillo/export_list.html",
        {"exports": page_obj, "page_obj": page_obj, "ns": "urology"},
    )


@login_required
@_with_urology_export_mappings
def export_new(request):
    project = _current_export_project(request)
    if project is None:
        messages.error(request, "Select a project before creating an export.")
        return redirect("urology:patient_list")

    if request.method == "POST":
        folder_ids = [int(fid) for fid in request.POST.getlist("folder_ids")]
        artifact_keys = request.POST.getlist("artifacts")
        filters = export_catalog.filters_from_form(request.POST)

        if not folder_ids:
            messages.error(request, "Please select at least one folder.")
            return redirect("urology:export_new")

        if (
            Folder.objects.filter(id__in=folder_ids, project=project).count()
            != len(set(folder_ids))
        ):
            messages.error(request, "Select folders from the current project only.")
            return redirect("urology:export_new")

        allowed_keys = export_ui.allowed_artifact_keys("urology", project)
        artifact_keys = [key for key in artifact_keys if key in allowed_keys]
        if not artifact_keys:
            messages.error(request, "Please select at least one artifact to export.")
            return redirect("urology:export_new")

        artifacts = export_catalog.resolve_artifacts("urology", artifact_keys)
        query_params = {
            "domain": "urology",
            "project_id": project.id,
            "folder_ids": folder_ids,
            "artifacts": artifact_keys,
            "filters": filters,
            "modality_slugs": sorted(export_catalog.modality_slugs_for(artifacts)),
        }

        summary_parts = [
            f"{len(folder_ids)} folder{'s' if len(folder_ids) != 1 else ''}"
        ]
        summary_parts.append(", ".join(a.label for a in artifacts) or "nothing")
        described = export_catalog.describe_filters(
            "urology",
            project,
            [m.slug for m in export_ui.project_modalities(project)],
            filters,
        )
        if described:
            summary_parts.append(", ".join(described))

        export = Export.objects.create(
            user=request.user,
            status="pending",
            query_params=query_params,
            query_summary=", ".join(summary_parts),
        )

        start_export_processing(export.id, "urology")
        messages.success(request, f"Export #{export.id} created and processing started.")
        return redirect("urology:export_list")

    folders = export_ui.folder_tree(
        filter_folders_for_user(
            request.user,
            Folder.objects.filter(project=project).order_by("name"),
            "urology",
        ),
        Patient,
        "urology",
    )
    visible_folder_ids = [entry["folder"].id for entry in folders]
    patients_in_scope = Patient.objects.filter(folder_id__in=visible_folder_ids)
    modalities = export_ui.project_modalities(project)

    return render(
        request,
        "maxillo/export_new.html",
        {
            "project": project,
            "folders": folders,
            "modalities": modalities,
            "artifact_groups": export_ui.artifact_groups(
                "urology", project, patients_in_scope, patient_fk="urology_patient"
            ),
            "filter_groups": export_ui.grouped_filters(
                "urology", project, [m.slug for m in modalities]
            ),
            "ns": "urology",
        },
    )


@login_required
@_with_urology_export_mappings
def export_preview(request):
    try:
        data = (
            (_json.loads(request.body) if request.body else {})
            if request.method == "POST"
            else request.GET
        )

        folder_ids = data.get("folder_ids", [])
        if isinstance(folder_ids, str):
            folder_ids = [fid for fid in folder_ids.split(",") if fid]
        folder_ids = [int(fid) for fid in folder_ids if str(fid).strip()]

        artifact_keys = data.get("artifacts", [])
        if isinstance(artifact_keys, str):
            artifact_keys = [key for key in artifact_keys.split(",") if key]

        query_params = {
            "domain": "urology",
            "folder_ids": folder_ids,
            "artifacts": list(artifact_keys),
            "filters": data.get("filters", {}),
        }

        proc = ExportProcessor(
            Export(user=request.user, query_params=query_params), domain="urology"
        )
        patients = proc.query_patients()
        patient_count = patients.count()
        if patient_count:
            files, total_size = proc.collect_files(patients)
            file_count = len(files)
        else:
            file_count, total_size = 0, 0

        return JsonResponse(
            {
                "success": True,
                "patient_count": patient_count,
                "folder_count": len(folder_ids),
                "modality_count": len(proc.modality_slugs),
                "artifact_count": len(proc.artifacts),
                "file_count": file_count,
                "estimated_size": format_file_size(total_size),
                "estimated_size_bytes": total_size,
            }
        )
    except Exception as e:
        logger.error(f"Error in urology export_preview: {e}", exc_info=True)
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
def export_status(request, export_id):
    export = get_object_or_404(Export, id=export_id)
    if export.user != request.user and not user_is_project_admin(request.user, "urology"):
        return JsonResponse({"error": "Permission denied"}, status=403)

    export = _recover_stuck_export(export)

    data = {
        "id": export.id,
        "status": export.status,
        "query_summary": export.query_summary,
    }
    if export.status == "completed":
        data["file_size"] = export.file_size
        data["file_size_human"] = format_file_size(export.file_size)
        data["patient_count"] = export.patient_count
        if export.completed_at:
            data["completed_at"] = export.completed_at.isoformat()
    if export.status == "failed":
        data["error_message"] = export.error_message
    if export.status == "processing":
        if export.started_at:
            data["started_at"] = export.started_at.isoformat()
        if export.patient_count:
            data["patient_count"] = export.patient_count
        if export.progress_message:
            data["progress_message"] = export.progress_message
        if export.progress_percent is not None:
            data["progress_percent"] = export.progress_percent
    return JsonResponse(data)


@login_required
def export_download(request, export_id):
    export = get_object_or_404(Export, id=export_id)

    if export.user != request.user and not user_is_project_admin(request.user, "urology"):
        messages.error(request, "You do not have permission to download this export.")
        return redirect("urology:export_list")

    if export.status != "completed":
        messages.error(request, "Export is not yet completed.")
        return redirect("urology:export_list")

    if not export.file_path or not artifact_exists(export.file_path):
        messages.error(request, "Export file not found.")
        export.mark_failed("Export file not found in storage")
        return redirect("urology:export_list")

    filename = (
        os.path.basename((export.file_path or "").rstrip("/"))
        or f"export_{export.id}.zip"
    )
    return streaming_response(
        path_or_key=export.file_path,
        content_type="application/zip",
        filename=filename,
        as_attachment=True,
    )


@login_required
@require_POST
def export_share_update(request, export_id):
    export = get_object_or_404(Export, id=export_id)

    if export.user != request.user and not user_is_project_admin(request.user, "urology"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if export.status != "completed":
        return JsonResponse(
            {"success": False, "error": "Only completed exports can be shared"}, status=400
        )

    try:
        data = _json.loads(request.body) if request.body else request.POST
    except ValueError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )

    share_mode = (data.get("share_mode") or "").strip()
    if share_mode not in ("private", "authenticated", "public"):
        return JsonResponse({"success": False, "error": "Invalid share mode"}, status=400)

    regenerate_raw = data.get("regenerate", False)
    regenerate = (
        regenerate_raw
        if isinstance(regenerate_raw, bool)
        else str(regenerate_raw).lower() in ("1", "true", "yes")
    )

    export.share_mode = share_mode
    if share_mode == "private":
        export.share_token = None
        export.shared_at = None
        export.expires_at = None
        export.save(
            update_fields=["share_mode", "share_token", "shared_at", "expires_at"]
        )
        return JsonResponse(
            {
                "success": True,
                "share_mode": export.share_mode,
                "share_url": None,
                "expires_at": None,
            }
        )

    expires_at, expiry_error = resolve_share_expiry(
        data.get("expires_in_days"),
        current=export.expires_at,
        can_set_never=request.user.is_staff
        or user_is_project_admin(request.user, "urology"),
    )
    if expiry_error:
        return JsonResponse({"success": False, "error": expiry_error}, status=400)

    if regenerate or not export.share_token:
        export.ensure_share_token(force_new=regenerate)
    export.shared_at = timezone.now()
    export.expires_at = expires_at
    export.save(update_fields=["share_mode", "shared_at", "expires_at"])

    return JsonResponse(
        {
            "success": True,
            "share_mode": export.share_mode,
            "share_url": _build_shared_download_url(request, export.share_token),
            "expires_at": export.expires_at.isoformat() if export.expires_at else None,
        }
    )


@require_http_methods(["GET"])
def export_shared_landing(request, share_token):
    export, is_available, reason = _urology_shared_export_availability(share_token)
    if (
        export
        and export.share_mode == "authenticated"
        and not request.user.is_authenticated
    ):
        return redirect_to_login(request.get_full_path())
    return render(
        request,
        "maxillo/export_shared_landing.html",
        {
            "ns": "urology",
            "export": export,
            "is_available": is_available,
            "is_expired": reason == "expired",
            "share_token": share_token,
            "file_size_human": format_file_size(export.file_size)
            if export and export.file_size
            else None,
        },
        status=410 if reason == "expired" else 200,
    )


@require_http_methods(["GET"])
def export_shared_download(request, share_token):
    export, is_available, reason = _urology_shared_export_availability(share_token)
    if reason == "expired":
        return HttpResponseGone("This share link has expired.")
    if not export or not is_available:
        raise Http404("Export is not available.")
    if export.share_mode == "authenticated" and not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    filename = (
        os.path.basename((export.file_path or "").rstrip("/"))
        or f"export_{export.id}.zip"
    )
    return streaming_response(
        path_or_key=export.file_path,
        content_type="application/zip",
        filename=filename,
        as_attachment=True,
    )


@login_required
@require_POST
def export_delete(request, export_id):
    export = get_object_or_404(Export, id=export_id)
    if export.user != request.user and not user_is_project_admin(request.user, "urology"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    file_path = export.file_path
    deleted_count, _ = Export.objects.filter(id=export_id).delete()
    if not deleted_count:
        return JsonResponse(
            {"success": False, "error": "Export not found or already deleted."}, status=404
        )

    if file_path:
        try:
            get_object_storage().delete(file_path)
        except Exception as e:
            logger.warning(f"Could not delete export file {file_path}: {e}")

    return JsonResponse({"success": True})


@login_required
@require_POST
def export_stop(request, export_id):
    export = get_object_or_404(Export, id=export_id)
    if export.user != request.user and not user_is_project_admin(request.user, "urology"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    if export.status not in {"processing", "pending"}:
        return JsonResponse(
            {
                "success": False,
                "error": f"Export is not running (status: {export.status}).",
            },
            status=409,
        )

    killed_pids = _kill_export_processes(export.id)

    deleted_keys = []
    warnings = []
    storage = get_object_storage()

    if export.file_path:
        try:
            storage.delete(export.file_path)
            deleted_keys.append(export.file_path)
        except Exception as e:
            warnings.append(f"Could not delete {export.file_path}: {e}")

    prefix = f"exports/export_{export.id}_"
    try:
        for key in storage.list_keys(prefix):
            if not key.startswith(prefix) or not key.endswith(".zip"):
                continue
            try:
                storage.delete(key)
                deleted_keys.append(key)
            except Exception as e:
                warnings.append(f"Could not delete {key}: {e}")
    except Exception as e:
        warnings.append(f"Could not list keys for prefix {prefix}: {e}")

    who = getattr(request.user, "username", "unknown")
    stopped_at = timezone.now().strftime("%Y-%m-%d %H:%M:%S %Z")
    message = f"Stopped manually by {who} at {stopped_at}."
    if killed_pids:
        message += f" Killed worker PID(s): {', '.join(str(p) for p in killed_pids)}."
    if deleted_keys:
        message += f" Deleted {len(set(deleted_keys))} ZIP object(s)."
    export.mark_failed(message)

    return JsonResponse(
        {
            "success": True,
            "killed_pids": killed_pids,
            "deleted_keys": sorted(set(deleted_keys)),
            "warnings": warnings,
            "status": "failed",
            "error_message": message,
        }
    )

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, Client
from django.urls import reverse

from common.models import Modality, Project, ProjectAccess
from urology.models import Folder, Patient, Tag, UrologyProject


class UrologyDomainTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser(
            username="urology_admin",
            email="urology_admin@example.com",
            password="password123",
        )
        call_command("setup_urology_modalities")
        self.project = Project.objects.get(slug="urology")
        ProjectAccess.objects.get_or_create(
            user=self.user, project=self.project, defaults={"role": "admin"}
        )
        self.folder = Folder.objects.create(
            name="Test Folder", project=self.project, created_by=self.user
        )
        self.patient = Patient.objects.create(
            name="Test Patient 1",
            folder=self.folder,
            project=self.project,
            uploaded_by=self.user,
        )
        self.client.login(username="urology_admin", password="password123")

    def test_setup_urology_modalities(self):
        self.assertEqual(self.project.modalities.count(), 2)
        slugs = set(self.project.modalities.values_list("slug", flat=True))
        self.assertIn("urology-mri", slugs)
        self.assertIn("urology-wsi", slugs)

    def test_urology_root_redirect(self):
        response = self.client.get("/urology/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/urology/patients/", response.headers["Location"])

    def test_urology_patient_list_view(self):
        response = self.client.get("/urology/patients/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Patient 1")
        self.assertContains(response, "Urology MRI")
        self.assertContains(response, "Urology WSI")

    def test_urology_filter_by_folder(self):
        response = self.client.get(f"/urology/patients/?folder={self.folder.id}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Patient 1")

    def test_urology_placeholders(self):
        upload_resp = self.client.get("/urology/upload/")
        self.assertEqual(upload_resp.status_code, 200)
        self.assertContains(upload_resp, "Upload Scan")

        detail_resp = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(detail_resp.status_code, 200)
        self.assertContains(detail_resp, f"Patient {self.patient.patient_id}")

    def test_urology_folder_operations(self):
        create_resp = self.client.post(
            "/urology/folders/create/",
            data='{"name": "New Urology Folder"}',
            content_type="application/json",
        )
        self.assertEqual(create_resp.status_code, 200)
        self.assertTrue(Folder.objects.filter(name="New Urology Folder").exists())

    def test_urology_tag_operations(self):
        add_tag_resp = self.client.post(
            f"/urology/patient/{self.patient.patient_id}/tags/add/",
            data='{"tag": "biopsy-confirmed"}',
            content_type="application/json",
        )
        self.assertEqual(add_tag_resp.status_code, 200)
        self.assertIn("biopsy-confirmed", self.patient.tag_names())

    def test_urology_export_views(self):
        resp = self.client.get("/urology/export/")
        self.assertEqual(resp.status_code, 200)

        # Set session current project
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

        new_resp = self.client.get("/urology/export/new/")
        self.assertEqual(new_resp.status_code, 200)

    def test_urology_profile_view(self):
        resp = self.client.get("/urology/profile/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "urology_admin")

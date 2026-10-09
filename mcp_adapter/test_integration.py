import unittest
from mcp_adapter.integration import capabilities,module_status,operation_profile

class EasyChangeAdapterTests(unittest.TestCase):
    def test_metadata_independent_of_esp(self):
        self.assertFalse(module_status(esp_enabled=False)["esp_selected"])
        self.assertFalse(module_status(esp_enabled=False)["session_ready"])
    def test_profiles_are_real_project_registry(self):
        self.assertIn("read",capabilities()["operations"])
        self.assertEqual(operation_profile("read")["family"],"read")
    def test_write_needs_permission(self):
        self.assertTrue(operation_profile("write_file")["requires_permission"])
    def test_db_write_is_conditional(self):
        self.assertTrue(operation_profile("db_query",allow_write=True)["requires_permission"])

if __name__=="__main__":
    unittest.main()

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_HTML = PROJECT_ROOT / "地址智能标准化_前端.html"


class BatchFrontendHtmlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = FRONTEND_HTML.read_text(encoding="utf-8")

    def test_batch_paste_mode_can_hide_address_column(self) -> None:
        self.assertIn('id="batchAddressColumnWrapper"', self.html)
        self.assertIn(
            "const batchAddressColumnWrapper = document.getElementById('batchAddressColumnWrapper');",
            self.html,
        )
        self.assertIn(
            "batchAddressColumnWrapper.classList.toggle('hidden', !uploadOn);",
            self.html,
        )

    def test_batch_layout_supports_page_scroll(self) -> None:
        self.assertIn(
            'class="bg-bglight text-gray-900 font-sans min-h-screen w-screen flex items-start justify-center p-2 lg:p-5"',
            self.html,
        )
        self.assertIn(
            'class="w-full max-w-7xl h-auto bg-surface shadow-2xl overflow-visible rounded-xl lg:rounded-none border border-gray-100 flex flex-col"',
            self.html,
        )
        self.assertIn(
            '<section id="batchView" class="hidden bg-darkvault text-white noise-bg">',
            self.html,
        )
        self.assertIn('<div class="w-full flex flex-col lg:flex-row">', self.html)

    def test_batch_file_preview_flow_is_wired(self) -> None:
        self.assertIn(
            'id="batchParseBtn"',
            self.html,
        )
        self.assertIn(
            'id="batchPreviewTextarea"',
            self.html,
        )
        self.assertIn(
            'fetch(`${API_BASE_URL}/api/normalize/batch/preview`',
            self.html,
        )
        self.assertIn(
            "batchParseBtn.addEventListener('click', parseBatchFile);",
            self.html,
        )
        self.assertIn(
            "batchPreviewTextarea.addEventListener('input', updateBatchPreviewCount);",
            self.html,
        )
        self.assertIn(
            "formData.append('text', getEditedPreviewText());",
            self.html,
        )
        self.assertNotIn(
            "if (hasFile) formData.append('file', batchSelectedFile);",
            self.html,
        )


if __name__ == "__main__":
    unittest.main()

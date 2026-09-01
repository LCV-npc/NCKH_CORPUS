from dataclasses import dataclass, field
from typing import List, Optional, Any
import time

@dataclass
class ProcessingStep:
    step_name: str
    status: str = "pending"
    error: Optional[str] = None
    start_time: float = 0.0
    end_time: float = 0.0

    def start(self):
        self.status = "running"
        self.start_time = time.time()

    def complete(self, success: bool = True, error: str = None):
        self.status = "success" if success else "failed"
        self.error = error
        self.end_time = time.time()

@dataclass
class ExtractedMetadata:
    source: str
    file_path: str
    file_hash_sha256: str = ""
    steps_completed: List[str] = field(default_factory=list)
    processing_steps: List[ProcessingStep] = field(default_factory=list)
    extracted_text: str = ""
    title: str = ""
    authors: str = ""
    abstract: str = ""
    page_count: int = 0
    output_directory: str = ""
    metadata_file: str = ""
    extracted_files: List[Any] = field(default_factory=list)
    headings: List[Any] = field(default_factory=list)
    document_blocks: List[Any] = field(default_factory=list)
    sections: List[Any] = field(default_factory=list)
    validation_report: Any = field(default_factory=dict)
    language_decision: Any = field(default_factory=dict)
    structured_document_file: str = ""
    is_duplicate: bool = False
    duplicate_of: Any = field(default_factory=dict)
    keywords: List[str] = field(default_factory=list)
    affiliations: List[str] = field(default_factory=list)
    extraction: Any = field(default_factory=dict)

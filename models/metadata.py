from dataclasses import dataclass, field
from typing import List, Optional
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
    extracted_files: List[str] = field(default_factory=list)

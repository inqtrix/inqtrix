"""Load the OpenAI client's resource modules at import, not at the first call.

``AsyncOpenAI`` imports the modules of a resource lazily, at its first use. The
first chat completion or responses call of a process would spend that import
(about 40 and 60 ms) on the event loop that every run and request shares. Each
adapter built on the OpenAI client imports this module, so the cost falls at
startup instead. The responses resource imports the chat completions with it.
"""

import openai.resources.responses  # noqa: F401

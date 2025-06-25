# BatchHandler Usage Guide

## Overview

The `BatchHandler` class provides a streamlined interface for working with OpenAI's Batch API, supporting both chat completions and embeddings with automatic file management, progress tracking, and error handling.

## Key Features

- **Simple API**: Single method calls for batch processing
- **Flexible Usage**: Can be used as a context manager or standard object
- **Automatic Cleanup**: Manages local and remote files automatically
- **Progress Tracking**: Visual feedback with progress bars
- **Error Handling**: Retry logic for transient failures
- **Type Safety**: Full type annotations for better IDE support
- **Configurable**: Customizable timeouts, retry settings, and behavior

## Usage Examples

### Chat Completions

```python
with BatchHandler(client, "gpt-4o-mini") as handler:
    conversations = [[{"role": "user", "content": "What is AI?"}], [{"role": "user", "content": "Explain ML"}]]
    results = handler.process_chat_batch(conversations, temperature=0.7, max_tokens=150)
```

### Embeddings

```python
with BatchHandler(client, "text-embedding-3-small", endpoint="/v1/embeddings") as handler:
    texts = ["Text 1", "Text 2", "Text 3"]
    results = handler.process_embeddings_batch(texts, custom_ids=["id1", "id2", "id3"], dimensions=512)
```

### Advanced Configuration

```python
with BatchHandler(
    client=client, model="gpt-4o-mini", max_retries=5, retry_delay=2.0, poll_interval=30, show_progress=True
) as handler:
    results = handler.process_chat_batch(large_dataset)
```

## Installation and Setup

```python
import openai
from aizk.utilities.batch_handler import BatchHandler

# Initialize OpenAI client
client = openai.OpenAI(api_key="your-api-key")
```

## Usage Patterns

### Context Manager Usage (Recommended)

The context manager automatically handles cleanup of local and remote files:

```python
# Chat completions with context manager
with BatchHandler(client, "gpt-4o-mini") as handler:
    conversations = [
        [{"role": "user", "content": "What is AI?"}],
        [{"role": "user", "content": "Explain machine learning"}],
    ]
    results = handler.process_chat_batch(conversations)
    print(results)
# Files are automatically cleaned up here
```

### Standard Object Usage

You can also use BatchHandler as a regular object with manual cleanup:

```python
# Create handler
handler = BatchHandler(client, "gpt-4o-mini")

# Process batch
conversations = [
    [{"role": "user", "content": "What is AI?"}],
    [{"role": "user", "content": "Explain machine learning"}],
]
results = handler.process_chat_batch(conversations)

# Manual cleanup (recommended)
handler.cleanup()
```

### No Cleanup Usage

If you want to manage files yourself:

```python
# Disable auto-cleanup
handler = BatchHandler(client, "gpt-4o-mini", auto_cleanup=False)
results = handler.process_chat_batch(conversations)
# Files remain on OpenAI servers for manual management
```

## Chat Completions

### Basic Usage

```python
import openai
from aizk.utilities.batch_handler import BatchHandler

client = openai.OpenAI()

with BatchHandler(client, "gpt-4o-mini") as handler:
    # Each conversation is a list of messages
    conversations = [
        [{"role": "user", "content": "What is artificial intelligence?"}],
        [
            {"role": "user", "content": "Explain quantum computing"},
            {"role": "assistant", "content": "Quantum computing uses quantum mechanics..."},
            {"role": "user", "content": "What are the main applications?"},
        ],
        [{"role": "user", "content": "How does machine learning work?"}],
    ]

    results = handler.process_chat_batch(conversations)

    # Results are organized by custom_id
    for custom_id, responses in results.items():
        print(f"Conversation {custom_id}:")
        for response in responses:
            print(f"  {response['response']['body']['choices'][0]['message']['content']}")
```

### With Custom IDs

```python
with BatchHandler(client, "gpt-4o-mini") as handler:
    conversations = [
        [{"role": "user", "content": "What is AI?"}],
        [{"role": "user", "content": "What is ML?"}],
    ]

    custom_ids = ["ai_question", "ml_question"]

    results = handler.process_chat_batch(conversations, custom_ids=custom_ids)

    # Access results by your custom IDs
    ai_response = results["ai_question"]
    ml_response = results["ml_question"]
```

### With Completion Parameters

```python
with BatchHandler(client, "gpt-4o-mini") as handler:
    conversations = [
        [{"role": "user", "content": "Write a creative story"}],
        [{"role": "user", "content": "Explain complex topics simply"}],
    ]

    results = handler.process_chat_batch(conversations, temperature=0.8, max_tokens=200, top_p=0.9)
```

## Embeddings

### Basic Usage

```python
with BatchHandler(client, "text-embedding-3-small", endpoint="/v1/embeddings") as handler:
    texts = ["Hello world", "How are you doing today?", "The weather is nice", "Machine learning is fascinating"]

    results = handler.process_embeddings_batch(texts)

    # Results contain embeddings for each text
    for custom_id, responses in results.items():
        embedding = responses[0]["response"]["body"]["data"][0]["embedding"]
        print(f"Text {custom_id}: {len(embedding)} dimensions")
```

### With Custom IDs and Parameters

```python
with BatchHandler(client, "text-embedding-3-large", endpoint="/v1/embeddings") as handler:
    texts = [
        "Document about artificial intelligence",
        "Research paper on quantum computing",
        "Article about climate change",
    ]

    custom_ids = ["doc_ai", "paper_quantum", "article_climate"]

    results = handler.process_embeddings_batch(
        texts,
        custom_ids=custom_ids,
        dimensions=1024,  # Reduce embedding dimensions
        encoding_format="float",
    )

    # Access embeddings by custom ID
    ai_embedding = results["doc_ai"][0]["response"]["body"]["data"][0]["embedding"]
```

## Configuration Options

### Basic Configuration

```python
handler = BatchHandler(
    client=client,
    model="gpt-4o-mini",
    endpoint="/v1/chat/completions",  # or "/v1/embeddings"
    batch_dir="./my_batches",  # Directory for batch files
    filename_prefix="my_batch",  # Prefix for generated files
)
```

### Advanced Configuration

```python
handler = BatchHandler(
    client=client,
    model="gpt-4o-mini",
    max_retries=5,  # Retry failed operations
    retry_delay=2.0,  # Base delay between retries
    poll_interval=30,  # Check batch status every 30s
    auto_cleanup=True,  # Automatically cleanup files
    show_progress=True,  # Show progress bars
)
```

### Without Progress Bars

```python
# For automated scripts where you don't want progress output
handler = BatchHandler(client=client, model="gpt-4o-mini", show_progress=False)
```

## Error Handling

The BatchHandler includes comprehensive error handling:

```python
from aizk.utilities.batch_handler import BatchHandler, BatchError, BatchValidationError, BatchProcessingError

try:
    with BatchHandler(client, "gpt-4o-mini") as handler:
        results = handler.process_chat_batch(conversations)
except BatchValidationError as e:
    print(f"Validation error: {e}")
except BatchProcessingError as e:
    print(f"Processing error: {e}")
except BatchError as e:
    print(f"General batch error: {e}")
```

## Large Dataset Processing

The BatchHandler automatically handles chunking for large datasets:

```python
# Handles large datasets automatically
large_conversations = [
    [{"role": "user", "content": f"Question {i}"}]
    for i in range(10000)  # 10k conversations
]

with BatchHandler(client, "gpt-4o-mini") as handler:
    # Automatically split into multiple batches if needed
    results = handler.process_chat_batch(large_conversations)

    print(f"Processed {len(results)} conversations")
```

## Best Practices

1. **Use Context Manager**: Always use the `with` statement for automatic cleanup
2. **Custom IDs**: Provide meaningful custom IDs for easier result processing
3. **Error Handling**: Wrap batch operations in try-catch blocks
4. **Large Datasets**: Let BatchHandler handle chunking automatically
5. **Configuration**: Adjust retry and polling settings based on your needs
6. **Progress Tracking**: Enable progress bars for long-running operations

## Common Patterns

### Processing Multiple File Types

```python
# Process different types of content
questions = [
    [{"role": "user", "content": "Summarize this research paper..."}],
    [{"role": "user", "content": "Translate this text..."}],
    [{"role": "user", "content": "Generate code for..."}],
]

custom_ids = ["summary_task", "translation_task", "coding_task"]

with BatchHandler(client, "gpt-4o-mini") as handler:
    results = handler.process_chat_batch(questions, custom_ids=custom_ids)

    summary = results["summary_task"]
    translation = results["translation_task"]
    code = results["coding_task"]
```

### Combining Chat and Embeddings

```python
# Process text for both chat and embeddings
texts = ["Document 1 content", "Document 2 content"]

# Get embeddings
with BatchHandler(client, "text-embedding-3-small", endpoint="/v1/embeddings") as handler:
    embeddings = handler.process_embeddings_batch(texts)

# Generate summaries
conversations = [[{"role": "user", "content": f"Summarize: {text}"}] for text in texts]

with BatchHandler(client, "gpt-4o-mini") as handler:
    summaries = handler.process_chat_batch(conversations)
```

## Monitoring and Debugging

### Check Batch Status

```python
handler = BatchHandler(client, "gpt-4o-mini", auto_cleanup=False)

# Process batch
results = handler.process_chat_batch(conversations)

# Check batch information
for batch_id, info in handler._batch_info.items():
    print(f"Batch {batch_id}: {info['status']}")

# Manual cleanup when done
handler.cleanup()
```

### Custom Logging

```python
import logging

# Enable detailed logging
logging.basicConfig(level=logging.INFO)

with BatchHandler(client, "gpt-4o-mini") as handler:
    results = handler.process_chat_batch(conversations)
# You'll see detailed logs about the batch processing
```

"""Text embedding tools for deAPI MCP server."""

from typing import Annotated, Any, List, Optional, Union

from pydantic import Field

from ..deapi_client import DeapiAPIError, get_client
from ..polling_manager import PollingManager
from ._responses import build_job_response, build_price_response


async def text_to_embedding(
    input: Annotated[
        Union[str, List[str]], Field(description="Text string or list of text strings to embed")
    ],
    model: Annotated[
        str, Field(description="Embedding model name (e.g., 'Bge_M3_FP16')")
    ] = "Bge_M3_FP16",
    return_result_in_response: Annotated[bool, Field(description="Return embedding inline")] = True,
    webhook_url: Annotated[
        Optional[str],
        Field(
            description="HTTPS URL for deAPI to call when this job finishes. Notifies your own systems; the tool still returns the result here"
        ),
    ] = None,
    webhook_secret: Annotated[
        Optional[str],
        Field(
            description="32-255 char key deAPI signs the callback with (HMAC-SHA256). Needs webhook_url. A secret given in chat stays in the transcript"
        ),
    ] = None,
) -> dict[str, Any]:
    """Generate text embeddings using AI models.

    Converts text into vector embeddings for semantic search, similarity comparison,
    and other NLP tasks. Accepts a single string or a list of strings.

    Returns:
        dict: Contains 'success', 'job_id' and the embeddings: 'result' when returned
              inline, otherwise 'result_url' pointing at the file that holds them
    """
    try:
        client = get_client()
        async with client:
            request_data = {
                "input": input,
                "model": model,
                "return_result_in_response": return_result_in_response,
            }

            job_response = await client.submit_job(
                endpoint="embeddings",
                json_data=request_data,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="embedding")
            result = await polling_manager.poll_until_complete(job_id)

            # With return_result_in_response=False the vectors go to a file and
            # only result_url carries them, the same as a transcript.
            return build_job_response(result, job_id)

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def text_to_embedding_price(
    input: Annotated[
        Union[str, List[str]],
        Field(description="Text string or list of text strings for price calculation"),
    ],
    model: Annotated[str, Field(description="Embedding model name")] = "Bge_M3_FP16",
) -> dict[str, Any]:
    """Calculate price for text embedding generation.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            request_data = {
                "input": input,
                "model": model,
            }

            price_response = await client.calculate_price(
                endpoint="embeddings/price",
                json_data=request_data,
            )

            return build_price_response(price_response)

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}

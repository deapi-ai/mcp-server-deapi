"""Text embedding tools for deAPI MCP server."""

from typing import Annotated, Optional, Union, List

from pydantic import Field

from ..deapi_client import get_client, DeapiAPIError
from ..polling_manager import PollingManager


async def text_to_embedding(
    input: Annotated[Union[str, List[str]], Field(description="Text string or list of text strings to embed")],
    model: Annotated[str, Field(description="Embedding model name (e.g., 'Bge_M3_FP16')")] = "Bge_M3_FP16",
    return_result_in_response: Annotated[bool, Field(description="Return embedding inline")] = True,
) -> dict:
    """Generate text embeddings using AI models.

    Converts text into vector embeddings for semantic search, similarity comparison,
    and other NLP tasks. Accepts a single string or a list of strings.

    Returns:
        dict: Contains 'success', 'result' with embedding vectors, 'job_id'
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
                endpoint="txt2embedding",
                json_data=request_data,
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="embedding")
            result = await polling_manager.poll_until_complete(job_id)

            if result.success:
                return {
                    "success": True,
                    "result": result.result,
                    "job_id": job_id,
                    "metadata": result.metadata,
                }
            else:
                return {
                    "success": False,
                    "error": result.error,
                    "job_id": job_id,
                }

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def text_to_embedding_price(
    input: Annotated[Union[str, List[str]], Field(description="Text string or list of text strings for price calculation")],
    model: Annotated[str, Field(description="Embedding model name")] = "Bge_M3_FP16",
) -> dict:
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
                endpoint="txt2embedding/price-calculation",
                json_data=request_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}

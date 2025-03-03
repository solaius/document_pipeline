from elasticsearch import AsyncElasticsearch
from typing import Optional
import json
from datetime import datetime, UTC
import redis

from ..models.document import Document, DocumentStatus, DocumentChunk
from ..config.settings import settings
from ..utils.logging import logger

class DocumentStorage:
    def __init__(self):
         # Ensure scheme is explicitly added
        es_host = settings.ELASTICSEARCH_HOST
        if not es_host.startswith("http://") and not es_host.startswith("https://"):
            scheme = "https" if settings.ELASTICSEARCH_USE_SSL else "http"
            es_host = f"{scheme}://{es_host}"
        
        self.es = AsyncElasticsearch(
            hosts=[f"{es_host}:{settings.ELASTICSEARCH_PORT}"],
            basic_auth=(
                settings.ELASTICSEARCH_USERNAME,
                settings.ELASTICSEARCH_PASSWORD
            ) if settings.ELASTICSEARCH_USERNAME else None
        )
        self.redis = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            password=settings.REDIS_PASSWORD if settings.REDIS_PASSWORD else None,
            decode_responses=True
        )
        self.index_name = "documents"
    
    async def initialize(self):
        if not await self.es.indices.exists(index=self.index_name):
            await self.create_index()
    
    async def create_index(self):
        settings = {
            "mappings": {
                "properties": {
                    "doc_id": {"type": "keyword"},
                    "filename": {"type": "keyword"},
                    "content_type": {"type": "keyword"},
                    "status": {"type": "keyword"},
                    "chunks": {
                        "type": "nested",
                        "properties": {
                            "chunk_id": {"type": "keyword"},
                            "content": {"type": "text"},
                            "page_number": {"type": "integer"},
                            "position": {"type": "object"},
                            "metadata": {"type": "object"}
                        }
                    },
                    "metadata": {"type": "object"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "error_message": {"type": "text"}
                }
            }
        }
        await self.es.indices.create(index=self.index_name, body=settings)
        logger.info(f"Created index: {self.index_name}")
    
    async def add_document(self, document: Document) -> None:
        doc = document.model_dump()
        doc["created_at"] = doc["created_at"].isoformat()
        doc["updated_at"] = doc["updated_at"].isoformat()
        
        # Store in Elasticsearch
        await self.es.index(
            index=self.index_name,
            id=document.doc_id,
            body=doc
        )
        
        # Cache in Redis with 1-hour expiry
        self.redis.setex(
            f"doc:{document.doc_id}",
            3600,  # 1 hour
            json.dumps(doc)
        )
        logger.info(f"Stored document: {document.doc_id}")
    
    async def update_document(self, document: Document) -> None:
        """Update a document's details in storage, including its chunks."""
        update_data = {
            "doc": {
                "status": document.status,
                "updated_at": datetime.now(UTC).isoformat(),
                "chunks": [chunk.model_dump() for chunk in document.chunks],
            }
        }

        # 🔹 Update in Elasticsearch
        await self.es.update(
            index=self.index_name,
            id=document.doc_id,
            body=update_data
        )

        # 🔹 Update Redis cache
        cached = self.redis.get(f"document:{document.doc_id}")
        if cached:
            doc_data = json.loads(cached)
            doc_data.update(update_data["doc"])
            self.redis.setex(
                f"document:{document.doc_id}",
                3600,  # 1 hour
                json.dumps(doc_data)
            )

        logger.info(f"Updated document: {document.doc_id}")

    async def get_document(self, doc_id: str) -> Optional[Document]:
        # Try Redis cache first
        cached = self.redis.get(f"document:{doc_id}")
        if cached:
            logger.info(f"Retrieved document from cache: {doc_id}")
            data = json.loads(cached)
            data["created_at"] = datetime.fromisoformat(data["created_at"])
            data["updated_at"] = datetime.fromisoformat(data["updated_at"])
            data["chunks"] = [DocumentChunk(**chunk) for chunk in data.get("chunks", [])]  # 🔹 Ensure chunks are retrieved
            return Document(**data)

        # Fallback to Elasticsearch
        try:
            doc = await self.es.get(index=self.index_name, id=doc_id)
            if doc["found"]:
                logger.info(f"Retrieved document from Elasticsearch: {doc_id}")
                data = doc["_source"]
                data["created_at"] = datetime.fromisoformat(data["created_at"])
                data["updated_at"] = datetime.fromisoformat(data["updated_at"])
                data["chunks"] = [DocumentChunk(**chunk) for chunk in data.get("chunks", [])]  # 🔹 Ensure chunks are retrieved
                return Document(**data)
        except Exception as e:
            logger.error(f"Error retrieving document {doc_id}: {str(e)}")

        return None

    
    async def update_document_status(
        self,
        doc_id: str,
        status: DocumentStatus,
        error_message: Optional[str] = None
    ) -> None:
        update_body = {
            "doc": {
                "status": status,
                "updated_at": datetime.now(UTC).isoformat(),
                "error_message": error_message
            }
        }
        
        # Update Elasticsearch
        await self.es.update(
            index=self.index_name,
            id=doc_id,
            body=update_body
        )
        
        # Update Redis if cached
        cached = self.redis.get(f"doc:{doc_id}")
        if cached:
            doc_data = json.loads(cached)
            doc_data.update(update_body["doc"])
            self.redis.setex(
                f"doc:{doc_id}",
                3600,  # 1 hour
                json.dumps(doc_data)
            )
        
        logger.info(f"Updated document status: {doc_id} -> {status}")
    
    async def close(self):
        await self.es.close()
        self.redis.close()

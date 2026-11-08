from fastapi import APIRouter

from app.api.v1 import (audit, continuous_learning, coverage, curriculum, documents, domains, graph, growth, knowledge,
                        learning, quality, sources, topics)

api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(domains.router)
api_v1.include_router(topics.router)
api_v1.include_router(sources.router)
api_v1.include_router(documents.router)
api_v1.include_router(knowledge.router)
api_v1.include_router(learning.router)
api_v1.include_router(growth.router)
api_v1.include_router(audit.router)
api_v1.include_router(curriculum.router)
api_v1.include_router(coverage.router)
api_v1.include_router(graph.router)
api_v1.include_router(quality.router)
api_v1.include_router(continuous_learning.router)

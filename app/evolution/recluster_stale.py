"""evolution/recluster_stale.py — 将单建议 cluster 回退为 pending，重新走聚类管道

用法（在容器内）：
  cd /app && python -m evolution.recluster_stale --dry-run   # 仅统计
  cd /app && python -m evolution.recluster_stale             # 执行回退 + 重聚类
"""
import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("evolution.recluster_stale")

from evolution.config import EvolutionConfig
from evolution.db import get_session
from evolution.models import EvolutionBufferItem
from evolution.buffer_pool import BufferPool
from evolution.clustering import SuggestionClusterer
from evolution.confirmation import ConfirmationJudge
from evolution.version_manager import VersionManager


def find_stale_single_clusters(session) -> list:
    """返回所有 suggestion_count=1 的 cluster 行。"""
    return session.query(EvolutionBufferItem).filter_by(
        item_type="cluster", suggestion_count=1
    ).all()


def revert_to_pending(session, items) -> int:
    """将单建议 cluster 回退为 pending。返回成功数。"""
    reverted = 0
    for item in items:
        payload = item.payload_json or {}
        suggestion_id = payload.get("suggestion_id") or item.item_key
        original_suggestion = payload

        # 如果 payload 中有完整的 suggestions 列表，取第一条
        suggestions = payload.get("suggestions", [])
        if suggestions:
            original_suggestion = suggestions[0]

        # 删除旧的 cluster 行
        session.delete(item)

        # 创建新的 pending 行
        sug = (original_suggestion.get("suggestion") or {})
        preview_texts = []
        text = sug.get("text", "")
        if text:
            preview_texts.append(text[:80])

        new_item = EvolutionBufferItem(
            item_type="pending",
            item_key=suggestion_id,
            target_skill_name=sug.get("target_skill", item.target_skill_name or ""),
            suggestion_count=1,
            avg_causal_strength=sug.get("causal_strength", item.avg_causal_strength or 0),
            consistency_rate=1.0,
            scene_tags_json=original_suggestion.get("scene_tags", item.scene_tags_json or {}),
            preview_texts_json=preview_texts,
            payload_json=original_suggestion,
        )
        session.add(new_item)
        reverted += 1

    session.commit()
    return reverted


def run_pipeline(cfg: EvolutionConfig):
    """运行 聚类 → 确认 → 过期清理。"""
    pool = BufferPool(cfg)
    vm = VersionManager(cfg)

    logger.info("=== Re-clustering ===")
    clusterer = SuggestionClusterer(cfg, pool)
    cluster_results = clusterer.process_pending()
    merged = sum(1 for r in cluster_results if r["action"] == "added_to_cluster")
    new = sum(1 for r in cluster_results if r["action"] == "new_cluster")
    logger.info(f"Clustering done: {merged} merged, {new} new clusters, {len(cluster_results)} total")

    logger.info("=== Confirmation ===")
    judge = ConfirmationJudge(cfg, pool, vm)
    confirmed = judge.check_all_clusters()
    logger.info(f"Confirmation done: {len(confirmed)} clusters confirmed")

    logger.info("=== Expiration ===")
    expired = pool.expire_old_suggestions()
    logger.info(f"Expired {expired} old suggestions")

    # 最终状态
    status = pool.get_status()
    logger.info(f"Buffer status after pipeline: pending={status['pending_count']}, "
                f"cluster={status['cluster_count']}, confirmed={status['confirmed_count']}, "
                f"expired={status['expired_count']}")


def main():
    parser = argparse.ArgumentParser(description="Recluster stale single-suggestion clusters")
    parser.add_argument("--dry-run", action="store_true", help="Only count, don't modify")
    args = parser.parse_args()

    cfg = EvolutionConfig()
    session = get_session()

    try:
        items = find_stale_single_clusters(session)
        logger.info(f"Found {len(items)} single-suggestion clusters")

        if args.dry_run:
            for item in items:
                target = item.target_skill_name or "?"
                causal = item.avg_causal_strength or 0
                logger.info(f"  would revert: id={item.id}, key={item.item_key}, target={target}, causal={causal:.2f}")
            return

        if not items:
            logger.info("Nothing to revert")
            return

        logger.info(f"Reverting {len(items)} clusters to pending...")
        reverted = revert_to_pending(session, items)
        logger.info(f"Reverted {reverted} items to pending")
    finally:
        session.close()

    # 运行聚类管道
    logger.info("Running clustering pipeline with new threshold...")
    run_pipeline(cfg)
    logger.info("Done!")


if __name__ == "__main__":
    main()

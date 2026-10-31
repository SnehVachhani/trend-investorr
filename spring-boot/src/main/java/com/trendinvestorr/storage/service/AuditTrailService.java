package com.trendinvestorr.storage.service;

import com.trendinvestorr.core.constants.AuditEventType;
import com.trendinvestorr.storage.entity.AuditLogEntity;
import com.trendinvestorr.storage.repository.AuditLogRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Append-only audit trail with SHA-256 checksum chaining for tamper detection.
 * Replaces Python {@code core.security.audit.AuditTrail}.
 *
 * <p>Each row carries a checksum derived from (prevChecksum || eventType || details || timestamp).
 * The previous checksum is the most recent row in the table. A break in the chain
 * signals tampering. Lookups go through {@link AuditLogRepository}; writes are append-only.
 */
@Service
public class AuditTrailService {

    private static final Logger log = LoggerFactory.getLogger(AuditTrailService.class);

    private final AuditLogRepository repository;
    private final AtomicReference<String> latestChecksum = new AtomicReference<>("");

    public AuditTrailService(AuditLogRepository repository) {
        this.repository = repository;
        // Seed latest checksum from the most recently inserted row.
        repository.findAllByOrderByTimestampDesc(org.springframework.data.domain.PageRequest.of(0, 1))
                .stream()
                .findFirst()
                .ifPresent(row -> latestChecksum.set(row.getChecksum() == null ? "" : row.getChecksum()));
    }

    /**
     * Append an audit entry. Uses the current latest chain checksum as the prev,
     * computes a new checksum, and persists the row.
     */
    @Transactional
    public void log(AuditEventType eventType, Map<String, Object> details) {
        try {
            String prev = latestChecksum.get();
            Instant ts = Instant.now();
            String newChecksum = sha256(prev + "|" + eventType + "|" + (details == null ? "" : details) + "|" + ts);

            AuditLogEntity row = new AuditLogEntity();
            row.setTimestamp(ts);
            row.setEventType(eventType.name());
            row.setDetails(details == null ? Map.of() : details);
            row.setIpAddress("");
            row.setChecksum(newChecksum);
            row.setPrevChecksum(prev);
            repository.save(row);

            latestChecksum.set(newChecksum);
        } catch (Exception e) {
            // Audit failures must never break the trading pipeline.
            log.error("Failed to write audit entry for {}: {}", eventType, e.getMessage(), e);
        }
    }

    /** Convenience overload with no details. */
    public void log(AuditEventType eventType) {
        log(eventType, Map.of());
    }

    /**
     * Verifies the checksum chain across all rows. Returns true if every row's
     * checksum matches the recomputed value over its predecessor.
     */
    public boolean verifyChain() {
        String prev = "";
        for (AuditLogEntity row : repository.findAllByOrderByTimestampDesc(
                org.springframework.data.domain.PageRequest.of(0, Integer.MAX_VALUE))) {
            String expected = sha256(prev + "|" + row.getEventType() + "|"
                    + (row.getDetails() == null ? "" : row.getDetails()) + "|" + row.getTimestamp());
            if (!expected.equals(row.getChecksum())) {
                log.warn("Audit chain broken at row {} (expected={} got={})",
                        row.getId(), expected, row.getChecksum());
                return false;
            }
            prev = row.getChecksum() == null ? "" : row.getChecksum();
        }
        return true;
    }

    private static String sha256(String input) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] hash = md.digest(input.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(hash);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 not available", e);
        }
    }
}

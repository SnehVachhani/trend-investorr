package com.trendinvestorr.core.constants;

/**
 * Position product codes. Port of {@code core.constants.ProductType}.
 */
public enum ProductType {
    INTRADAY,        // MIS — squared off same day
    DELIVERY,        // CNC — cash and carry, hold overnight
    CARRY_FORWARD    // NRML — F&O carry forward
}

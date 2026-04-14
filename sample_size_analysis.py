"""
Statistical Analysis: Sample Size Requirements for EV Betting Strategy
"""
import math

# Z-scores for common confidence levels
Z_SCORES = {
    0.90: 1.645,
    0.95: 1.96,
    0.99: 2.576
}

def norm_cdf(z):
    """Approximate normal CDF using error function"""
    # Approximation: 0.5 * (1 + erf(z/sqrt(2)))
    # Using simple approximation for z > 0
    if z < 0:
        return 1 - norm_cdf(-z)
    # Approximation for z >= 0
    t = 1 / (1 + 0.2316419 * abs(z))
    d = 0.3989423 * math.exp(-z * z / 2)
    prob = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
    return 1 - prob

def norm_ppf(p):
    """Approximate inverse normal CDF (percent point function)"""
    # Simple approximation for common values
    if p < 0.5:
        return -norm_ppf(1 - p)
    # Approximation for p >= 0.5
    if p > 0.999:
        return 3.0
    if p > 0.99:
        return 2.33
    if p > 0.975:
        return 1.96
    if p > 0.95:
        return 1.645
    if p > 0.90:
        return 1.28
    return 0.0

def calculate_sample_size_for_ev(ev_percent, confidence_level=0.95, power=0.80, 
                                  min_detectable_roi=0.02, bet_size_variance=0.5):
    """
    Calculate required sample size to detect if strategy is profitable
    
    Parameters:
    - ev_percent: Expected EV (e.g., 8% = 0.08)
    - confidence_level: Confidence level (default 95%)
    - power: Statistical power (default 80%)
    - min_detectable_roi: Minimum ROI we want to detect (default 2%)
    - bet_size_variance: Variance factor for bet sizes (0.5 = moderate variance)
    
    Returns:
    - Required sample size
    """
    # Z-scores for confidence and power
    z_alpha = norm_ppf(1 - (1 - confidence_level) / 2)  # Two-tailed
    z_beta = norm_ppf(power)
    
    # Estimate variance based on typical betting outcomes
    # For a bet with EV=8%, typical outcomes might be:
    # - Win: +100% return (1.0)
    # - Loss: -100% return (-1.0)
    # - Average win rate needed for 8% EV: ~54% at even odds
    
    # Simplified variance estimate
    # If we assume average odds around -110 (implied prob ~52.4%)
    # Win: +0.91 (at -110 odds)
    # Loss: -1.0
    # Expected return: 0.08
    # Variance per bet ≈ (0.91 - 0.08)^2 * 0.524 + (-1.0 - 0.08)^2 * 0.476
    # ≈ 0.69^2 * 0.524 + 1.08^2 * 0.476 ≈ 0.25 + 0.55 ≈ 0.80
    
    # Adjust for bet size variance
    variance_per_bet = 0.80 * (1 + bet_size_variance)
    
    # Standard error
    se = math.sqrt(variance_per_bet)
    
    # Effect size (difference we want to detect)
    effect_size = min_detectable_roi / se
    
    # Sample size calculation (two-sample t-test approximation)
    n = ((z_alpha + z_beta) ** 2 * variance_per_bet) / (min_detectable_roi ** 2)
    
    return int(math.ceil(n))


def calculate_confidence_interval(current_roi, n_bets, confidence=0.95):
    """
    Calculate confidence interval for current ROI
    """
    # Estimate standard error
    # Using conservative estimate of variance
    se = math.sqrt(0.80 / n_bets)  # Conservative variance estimate
    
    z_score = norm_ppf(1 - (1 - confidence) / 2)
    margin_of_error = z_score * se
    
    lower_bound = current_roi - margin_of_error
    upper_bound = current_roi + margin_of_error
    
    return lower_bound, upper_bound, margin_of_error


def analyze_current_performance():
    """
    Analyze current performance and sample size adequacy
    """
    print("=" * 80)
    print("SAMPLE SIZE ANALYSIS FOR EV BETTING STRATEGY")
    print("=" * 80)
    print()
    
    # Current stats from HTML
    current_bets = 431
    current_roi = 0.0538  # 5.38%
    expected_ev = 0.08  # 8% EV
    
    print(f"CURRENT PERFORMANCE:")
    print(f"   Total Bets: {current_bets}")
    print(f"   Current ROI: {current_roi:.2%}")
    print(f"   Expected EV: {expected_ev:.2%}")
    print(f"   Difference: {current_roi - expected_ev:.2%}")
    print()
    
    # Calculate confidence interval
    lower, upper, margin = calculate_confidence_interval(current_roi, current_bets)
    print(f"CONFIDENCE INTERVAL (95%):")
    print(f"   Lower Bound: {lower:.2%}")
    print(f"   Upper Bound: {upper:.2%}")
    print(f"   Margin of Error: +/-{margin:.2%}")
    print(f"   True ROI likely between: {lower:.2%} and {upper:.2%}")
    print()
    
    # Check if we can reject null hypothesis (ROI <= 0)
    se = math.sqrt(0.80 / current_bets)
    z_stat = current_roi / se
    p_value = 1 - norm_cdf(z_stat)
    print(f"STATISTICAL SIGNIFICANCE TEST:")
    print(f"   Z-statistic: {z_stat:.2f}")
    print(f"   P-value: {p_value:.4f}")
    if p_value < 0.05:
        print(f"   [YES] Statistically significant (p < 0.05) - Strategy is profitable!")
    else:
        print(f"   [WARNING] Not yet statistically significant (p >= 0.05)")
    print()
    
    # Calculate required sample sizes for different scenarios
    print("=" * 80)
    print("REQUIRED SAMPLE SIZES FOR DIFFERENT CONFIDENCE LEVELS")
    print("=" * 80)
    print()
    
    scenarios = [
        {"name": "Detect 2% ROI (minimum)", "min_roi": 0.02, "confidence": 0.95},
        {"name": "Detect 5% ROI (moderate)", "min_roi": 0.05, "confidence": 0.95},
        {"name": "Detect 8% ROI (target EV)", "min_roi": 0.08, "confidence": 0.95},
        {"name": "High confidence (99%)", "min_roi": 0.05, "confidence": 0.99},
    ]
    
    for scenario in scenarios:
        n_required = calculate_sample_size_for_ev(
            expected_ev, 
            confidence_level=scenario["confidence"],
            min_detectable_roi=scenario["min_roi"]
        )
        additional_needed = max(0, n_required - current_bets)
        print(f"{scenario['name']}:")
        print(f"   Required: {n_required:,} bets")
        print(f"   Additional needed: {additional_needed:,} bets")
        if current_bets >= n_required:
            print(f"   [YES] You have enough data!")
        else:
            completion = (current_bets / n_required) * 100
            print(f"   [WARNING] You have {completion:.1f}% of required sample")
        print()
    
    # Practical recommendations
    print("=" * 80)
    print("PRACTICAL RECOMMENDATIONS")
    print("=" * 80)
    print()
    
    # Rule of thumb: Need ~100 bets per 1% of ROI to detect
    rule_of_thumb = int(expected_ev * 100 * 100)
    print(f"RULE OF THUMB:")
    print(f"   For {expected_ev:.0%} EV, need ~{rule_of_thumb:,} bets for reliable detection")
    print(f"   You have {current_bets:,} bets ({current_bets/rule_of_thumb*100:.1f}% of rule of thumb)")
    print()
    
    # Account for variance in bet sizes and odds
    print(f"VARIANCE CONSIDERATIONS:")
    print(f"   - Higher variance (different bet sizes, odds) = need more bets")
    print(f"   - Lower variance (similar bets) = need fewer bets")
    print(f"   - Current estimate assumes moderate variance")
    print()
    
    # Time-based analysis
    print(f"TIME-BASED ANALYSIS:")
    print(f"   If betting ~{current_bets} bets represents your typical volume:")
    days_for_confidence = math.ceil(rule_of_thumb / current_bets)
    print(f"   - Need ~{days_for_confidence} similar days to reach confidence")
    print(f"   - Or ~{days_for_confidence * 7} days at current weekly pace")
    print()
    
    # Break-even analysis
    print(f"BREAK-EVEN ANALYSIS:")
    print(f"   To be 95% confident strategy is profitable (ROI > 0):")
    n_breakeven = calculate_sample_size_for_ev(expected_ev, min_detectable_roi=0.01)
    print(f"   Need ~{n_breakeven:,} bets")
    if current_bets >= n_breakeven:
        print(f"   [YES] You have enough data to confirm profitability!")
    else:
        print(f"   [WARNING] Need {n_breakeven - current_bets:,} more bets")
    print()
    
    # Filter-specific analysis
    print("=" * 80)
    print("FILTER-SPECIFIC ANALYSIS")
    print("=" * 80)
    print()
    
    filters = [
        {"name": "3 Sharps Filter", "bets": 157, "roi": 0.2467},
        {"name": "CBB Filter", "bets": 274, "roi": -0.0649},
    ]
    
    for f in filters:
        print(f"{f['name']}:")
        print(f"   Bets: {f['bets']}")
        print(f"   ROI: {f['roi']:.2%}")
        
        if f['roi'] > 0:
            lower, upper, margin = calculate_confidence_interval(f['roi'], f['bets'])
            print(f"   95% CI: {lower:.2%} to {upper:.2%}")
            
            # Check significance
            se = math.sqrt(0.80 / f['bets'])
            z_stat = f['roi'] / se
            p_value = 1 - norm_cdf(z_stat)
            if p_value < 0.05:
                print(f"   [YES] Statistically significant (p={p_value:.4f})")
            else:
                print(f"   [WARNING] Not yet significant (p={p_value:.4f})")
        else:
            print(f"   [NO] Negative ROI - not profitable")
        print()


if __name__ == "__main__":
    analyze_current_performance()

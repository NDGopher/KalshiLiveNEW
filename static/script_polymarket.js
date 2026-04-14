// Dark mode toggle
(function() {
    const darkModeToggle = document.getElementById('dark-mode-toggle');
    const darkModeIcon = document.getElementById('dark-mode-icon');
    
    // Load saved theme
    const savedTheme = localStorage.getItem('theme') || 'light';
    if (savedTheme === 'dark') {
        document.body.classList.add('dark');
        darkModeIcon.textContent = '☀️';
    }
    
    // Toggle theme
    if (darkModeToggle) {
        darkModeToggle.addEventListener('click', () => {
            document.body.classList.toggle('dark');
            const isDark = document.body.classList.contains('dark');
            darkModeIcon.textContent = isDark ? '☀️' : '🌙';
            localStorage.setItem('theme', isDark ? 'dark' : 'light');
        });
    }
})();

// Socket.IO connection
const socket = io();

// State
let alerts = new Map();
let portfolioBalance = null;
let maxBetAmount = 100.0;  // Default max bet amount

// Convert price in cents to American odds
function priceToAmericanOdds(priceCents) {
    if (!priceCents || priceCents <= 0 || priceCents >= 100) {
        return "N/A";
    }
    
    const price = priceCents / 100.0;
    
    if (price >= 0.5) {
        // Favorite (negative odds)
        const odds = -100 * price / (1 - price);
        return `${Math.round(odds)}`;
    } else {
        // Underdog (positive odds)
        const odds = 100 * (1 - price) / price;
        return `+${Math.round(odds)}`;
    }
}

// DOM elements
const statusIndicator = document.getElementById('status-indicator');
const statusText = document.getElementById('status-text');
const alertsList = document.getElementById('alerts-list');
const balanceElement = document.getElementById('balance');
const refreshBtn = document.getElementById('refresh-btn');
const maxBetInput = document.getElementById('max-bet-input');
const setMaxBetBtn = document.getElementById('set-max-bet-btn');
const minEvInput = document.getElementById('min-ev-input');
const setFiltersBtn = document.getElementById('set-filters-btn');

// Auto-bet DOM elements
const autoBetEnabled = document.getElementById('auto-bet-enabled');
const autoBetConfig = document.getElementById('auto-bet-config');
const autoBetEvMin = document.getElementById('auto-bet-ev-min');
const autoBetEvMax = document.getElementById('auto-bet-ev-max');
const autoBetOddsMin = document.getElementById('auto-bet-odds-min');
const autoBetOddsMax = document.getElementById('auto-bet-odds-max');
const autoBetAmount = document.getElementById('auto-bet-amount');
const autoBetNhlOversAmount = document.getElementById('auto-bet-nhl-overs-amount');
const saveAutoBetBtn = document.getElementById('save-auto-bet-btn');

// Filter selection DOM elements
const dashboardFiltersCheckboxes = document.getElementById('dashboard-filters-checkboxes');
const saveFilterSelectionBtn = document.getElementById('save-filter-selection-btn');

// Socket event handlers
socket.on('connect', () => {
    console.log('Connected to server');
    statusIndicator.className = 'status-dot connected';
    statusText.textContent = 'Connected';
    fetchPortfolio();
    loadFilterSettings();  // Load filter selections
    loadAutoBetSettings();  // Load auto-bet settings
});

socket.on('disconnect', () => {
    console.log('Disconnected from server');
    statusIndicator.className = 'status-dot disconnected';
    statusText.textContent = 'Disconnected';
});

socket.on('alerts_update', (data) => {
    console.log('Received alerts update:', data);
    if (data.alerts) {
        // Clear all alerts first, then add the new ones
        // This ensures frontend state matches server state after restart
        alerts.clear();
        data.alerts.forEach(alert => {
            alerts.set(alert.id, alert);
        });
        renderAlerts();
    }
});

socket.on('clear_all_alerts', () => {
    console.log('Clearing all alerts (server restart or empty state)');
    alerts.clear();
    renderAlerts();
});

socket.on('new_alert', (alert) => {
    console.log('New alert received:', alert);
    // Ensure ID is string for consistency
    alerts.set(String(alert.id), alert);
    renderAlerts();
    // Highlight new alert
    setTimeout(() => {
        const alertCard = document.querySelector(`[data-alert-id="${alert.id}"]`);
        if (alertCard) {
            alertCard.classList.remove('new');
        }
    }, 3000);
});

socket.on('bet_result', (data) => {
    console.log('Bet result:', data);
    const alertId = data.alert_id;
    const result = data.result;
    
    const alertCard = document.querySelector(`[data-alert-id="${alertId}"]`);
    if (!alertCard) return;
    
    const statusDiv = alertCard.querySelector('.bet-status');
    statusDiv.className = 'bet-status';
    
    if (result.success) {
        statusDiv.className = 'bet-status success';
        statusDiv.textContent = `✅ Bet placed! ${result.count} contracts at ${(result.price_cents / 100).toFixed(2)}¢`;
    } else if (result.error === 'Odds changed') {
        statusDiv.className = 'bet-status error';
        statusDiv.textContent = `❌ Odds changed! Expected ${result.expected}¢, got ${result.current}¢`;
    } else {
        statusDiv.className = 'bet-status error';
        statusDiv.textContent = `❌ Error: ${result.error || 'Unknown error'}`;
    }
    
    // Refresh portfolio
    fetchPortfolio();
});

socket.on('bet_confirmation', (data) => {
    console.log('Bet confirmation:', data);
    showToast(data.status, data.message, data.result);
});

socket.on('bet_success', (data) => {
    console.log('Bet success:', data);
    // Show popup with cost, American odds, and win amount
    showBetSuccessPopup(data);
});

socket.on('bet_error', (data) => {
    console.log('Bet error:', data);
    showToast('error', `Bet failed: ${data.error}`, null);
});

socket.on('remove_alert', (data) => {
    console.log('Remove alert:', data);
    const alertId = String(data.id);
    if (alerts.has(alertId)) {
        alerts.delete(alertId);
        renderAlerts();
        console.log(`🗑️  Removed alert ${alertId} from UI`);
    }
});

socket.on('auto_bet_placed', (data) => {
    console.log('Auto-bet placed:', data);
    // Show popup notification (same as manual bet success)
    showBetSuccessPopup({
        ...data,
        // Ensure all required fields are present
        cost: data.cost || 0,
        american_odds: data.american_odds || 'N/A',
        win_amount: data.win_amount || 0,
        fill_count: data.fill_count || 0,
        status: data.status || 'executed',
        market_name: data.market_name || data.market || data.teams || 'N/A',
        submarket_name: data.submarket_name || data.pick || 'N/A',
        ticker: data.ticker || 'N/A'
    });
    fetchPortfolio(); // Refresh balance
});

socket.on('auto_bet_failed', (data) => {
    console.log('Auto-bet failed:', data);
    showToast('error', `❌ Auto-bet failed: ${data.market} - ${data.error}`, null);
});

socket.on('orderbook_update', (data) => {
    console.log('Orderbook update:', data);
    // Update orderbook display if needed
});

socket.on('alert_update', (data) => {
    console.log('Alert update:', data);
    const alertId = String(data.id);
    const alert = alerts.get(alertId);
    if (alert) {
        // Store old values BEFORE updating (for comparison)
        const oldEv = alert.ev_percent;
        const oldLiquidity = alert.liquidity;
        const oldExpectedProfit = alert.expected_profit;
        const oldPriceCents = alert.price_cents;
        const oldBookPrice = alert.book_price;
        const oldAmericanOdds = alert.american_odds;
        
        // Update alert with new data
        if (data.price_cents !== undefined) alert.price_cents = data.price_cents;
        if (data.liquidity !== undefined) alert.liquidity = data.liquidity;
        if (data.book_price !== undefined) alert.book_price = data.book_price;
        if (data.ev_percent !== undefined) alert.ev_percent = data.ev_percent;
        if (data.expected_profit !== undefined) alert.expected_profit = data.expected_profit;
        if (data.odds !== undefined) alert.odds = data.odds;
        if (data.american_odds !== undefined) alert.american_odds = data.american_odds;
        if (data.display_books !== undefined) alert.display_books = data.display_books;
        if (data.devig_books !== undefined) alert.devig_books = data.devig_books;
        if (data.sharp_books !== undefined) alert.sharp_books = data.sharp_books;
        
        // Update the card in place instead of re-rendering everything
        const alertCard = document.querySelector(`[data-alert-id="${alertId}"]`);
        if (alertCard) {
            let needsRerender = false;
            let hasChanges = false;
            
            // Check if devig_books changed (affects graying - need re-render)
            if (data.devig_books !== undefined) {
                const oldDevigBooks = JSON.stringify(alert.devig_books || []);
                const newDevigBooks = JSON.stringify(data.devig_books);
                if (oldDevigBooks !== newDevigBooks) {
                    needsRerender = true;
                    hasChanges = true;
                }
            }
            
            // Check if sharp_books changed (affects which books are shown - need re-render)
            if (data.sharp_books !== undefined) {
                const oldSharpBooks = JSON.stringify(alert.sharp_books || []);
                const newSharpBooks = JSON.stringify(data.sharp_books);
                if (oldSharpBooks !== newSharpBooks) {
                    needsRerender = true;
                    hasChanges = true;
                }
            }
            
            // Check if display_books structure changed (books added/removed - need re-render)
            // But if only prices/limits changed, update in place
            if (data.display_books !== undefined) {
                const oldDisplayBooks = alert.display_books || {};
                const newDisplayBooks = data.display_books;
                
                // Check if book structure changed (books added/removed)
                const oldBookNames = new Set();
                const newBookNames = new Set();
                
                Object.keys(oldDisplayBooks).forEach(selection => {
                    (oldDisplayBooks[selection] || []).forEach(book => {
                        oldBookNames.add(book.book);
                    });
                });
                
                Object.keys(newDisplayBooks).forEach(selection => {
                    (newDisplayBooks[selection] || []).forEach(book => {
                        newBookNames.add(book.book);
                    });
                });
                
                // If books were added/removed, need full re-render
                if (oldBookNames.size !== newBookNames.size || 
                    [...oldBookNames].some(name => !newBookNames.has(name)) ||
                    [...newBookNames].some(name => !oldBookNames.has(name))) {
                    needsRerender = true;
                    hasChanges = true;
                } else {
                    // Only prices/limits changed - update in place
                    const ourSelection = alert.pick;
                    const ourBooks = newDisplayBooks[ourSelection] || [];
                    
                    ourBooks.forEach(book => {
                        const bookName = book.book;
                        const bookOdds = book.odds || 0;
                        const bookLimit = book.limit || 0;
                        const bookLimitDisplay = bookLimit ? `$${(bookLimit / 1000).toFixed(1)}k` : '';
                        
                        // Find the book cell by data attribute
                        const bookCell = alertCard.querySelector(`.book-cell[data-book-name="${bookName}"]`);
                        if (bookCell) {
                            // Update odds
                            const oddsEl = bookCell.querySelector('.book-odds');
                            if (oddsEl) {
                                const currentOdds = parseInt(oddsEl.getAttribute('data-book-odds') || '0');
                                if (currentOdds !== bookOdds) {
                                    oddsEl.setAttribute('data-book-odds', bookOdds);
                                    oddsEl.textContent = `${bookOdds > 0 ? '+' : ''}${bookOdds}`;
                                    hasChanges = true;
                                }
                            }
                            
                            // Update limit
                            const limitEl = bookCell.querySelector('.book-limit');
                            if (bookLimitDisplay) {
                                const currentLimit = parseFloat(limitEl?.getAttribute('data-book-limit') || '0');
                                if (Math.abs(currentLimit - bookLimit) > 0.01) {
                                    if (limitEl) {
                                        limitEl.setAttribute('data-book-limit', bookLimit);
                                        limitEl.textContent = bookLimitDisplay;
                                    } else {
                                        // Limit element doesn't exist, create it
                                        const limitDiv = document.createElement('div');
                                        limitDiv.className = 'book-limit';
                                        limitDiv.setAttribute('data-book-limit', bookLimit);
                                        limitDiv.textContent = bookLimitDisplay;
                                        bookCell.appendChild(limitDiv);
                                    }
                                    hasChanges = true;
                                }
                            } else if (limitEl) {
                                // Remove limit if it's now empty
                                limitEl.remove();
                                hasChanges = true;
                            }
                            
                            // Update graying status if limit changed (might affect graying)
                            const minSharpLimits = {
                                'BookMaker': 250, 'Circa': 250, 'Novig': 200, 'Pinnacle': 250,
                                'ProphetX': 200, 'SportTrade': 200, 'DraftKings': 250, 'FanDuel': 250
                            };
                            const devigBooks = alert.devig_books || [];
                            const shouldBeGrayed = bookName !== 'Polymarket' && (
                                !devigBooks.includes(bookName) || 
                                (minSharpLimits[bookName] && bookLimit < minSharpLimits[bookName])
                            );
                            const isCurrentlyGrayed = bookCell.classList.contains('grayed-out');
                            
                            if (shouldBeGrayed !== isCurrentlyGrayed) {
                                if (shouldBeGrayed) {
                                    bookCell.classList.add('grayed-out');
                                } else {
                                    bookCell.classList.remove('grayed-out');
                                }
                                hasChanges = true;
                            }
                            
                            // Update "better than Polymarket" indicator (red outline)
                            const polymarketPrice = alert.book_price || alert.american_odds || (alert.price_cents ? priceToAmericanOdds(alert.price_cents) : 'N/A');
                            let polymarketOddsNum = null;
                            if (polymarketPrice && polymarketPrice !== 'N/A') {
                                const polymarketStr = String(polymarketPrice).replace(/[+]/g, '');
                                polymarketOddsNum = parseInt(polymarketStr, 10);
                                if (isNaN(polymarketOddsNum)) {
                                    polymarketOddsNum = null;
                                }
                            }
                            
                            let hasBetterOdds = false;
                            if (shouldBeGrayed && polymarketOddsNum !== null && bookOdds !== 0) {
                                if (bookOdds > 0 && polymarketOddsNum > 0) {
                                    hasBetterOdds = bookOdds > polymarketOddsNum;
                                } else if (bookOdds < 0 && polymarketOddsNum < 0) {
                                    hasBetterOdds = bookOdds > polymarketOddsNum; // Less negative is better
                                } else if (bookOdds > 0 && polymarketOddsNum < 0) {
                                    hasBetterOdds = true;
                                }
                            }
                            
                            const currentlyHasBetter = bookCell.classList.contains('better-than-polymarket');
                            if (hasBetterOdds !== currentlyHasBetter) {
                                if (hasBetterOdds) {
                                    bookCell.classList.add('better-than-polymarket');
                                } else {
                                    bookCell.classList.remove('better-than-polymarket');
                                }
                                hasChanges = true;
                            }
                        }
                    });
                }
            }
            
            // Check if EV changed - ALWAYS update if value is provided (even small changes)
            if (data.ev_percent !== undefined) {
                const newEv = data.ev_percent;
                // Update even for tiny changes to ensure real-time updates
                if (Math.abs((oldEv || 0) - newEv) > 0.0001) {
                    hasChanges = true;
                    const evValueEl = alertCard.querySelector('.ev-value');
                    if (evValueEl) {
                        evValueEl.textContent = `${newEv >= 0 ? '+' : ''}${newEv.toFixed(2)}%`;
                        evValueEl.className = `ev-value ${newEv >= 0 ? 'positive' : 'negative'}`;
                        
                        // Update card background color based on EV (green only if >= 8%)
                        if (newEv >= 8.0) {
                            alertCard.classList.add('high-ev');
                        } else {
                            alertCard.classList.remove('high-ev');
                        }
                        console.log(`[FRONTEND] Updated EV: ${oldEv}% → ${newEv}%`);
                    } else {
                        console.warn(`[FRONTEND] EV element not found for alert ${alertId}`);
                    }
                }
            }
            
            // Check if expected profit changed
            if (data.expected_profit !== undefined) {
                const newExpectedProfit = data.expected_profit;
                if (Math.abs((oldExpectedProfit || 0) - newExpectedProfit) > 0.01) {
                    hasChanges = true;
                    // Expected profit is typically shown in the EV section or as a separate element
                    // Update if there's an element for it
                    const expectedProfitEl = alertCard.querySelector('.expected-profit');
                    if (expectedProfitEl) {
                        expectedProfitEl.textContent = `$${newExpectedProfit.toFixed(2)}`;
                    }
                }
            }
            
            // Check if price/book_price changed
            if (data.price_cents !== undefined || data.book_price !== undefined || data.american_odds !== undefined) {
                const newPriceCents = data.price_cents !== undefined ? data.price_cents : alert.price_cents;
                const newBookPrice = data.book_price !== undefined ? data.book_price : (data.american_odds !== undefined ? data.american_odds : alert.book_price);
                
                if (newPriceCents !== oldPriceCents || newBookPrice !== oldBookPrice) {
                    hasChanges = true;
                    // Update Polymarket price display if it exists
                    const polymarketPriceEl = alertCard.querySelector('.polymarket-price, .book-price');
                    if (polymarketPriceEl && newBookPrice) {
                        polymarketPriceEl.textContent = newBookPrice;
                    }
                }
            }
            
            // Check if liquidity changed (update BET MAX button and any liquidity display)
            if (data.liquidity !== undefined) {
                const newLiq = data.liquidity;
                if (Math.abs((oldLiquidity || 0) - newLiq) > 0.01) {  // Only update if liquidity changed by more than $0.01
                    hasChanges = true;
                    // Update BET MAX button if liquidity changed
                    const betMaxBtn = alertCard.querySelector('.btn-bet-max-bb');
                    if (betMaxBtn) {
                        const maxBet = Math.min(maxBetAmount, newLiq);
                        betMaxBtn.textContent = `BET MAX ($${maxBet.toFixed(0)})`;
                    }
                    // Update liquidity display if it exists
                    const liquidityEl = alertCard.querySelector('.liquidity, .polymarket-liquidity');
                    if (liquidityEl) {
                        liquidityEl.textContent = `$${(newLiq / 1000).toFixed(1)}k`;
                    }
                }
            }
            
            // Only re-render if structure changed (books added/removed or graying changed)
            if (needsRerender) {
                const newCard = createAlertCard(alert);
                alertCard.replaceWith(newCard);
                return; // Exit early since we re-rendered
            }
            
            // If no changes, don't update anything (prevents flashing)
            // We still update the alert data in memory, but don't touch the DOM
        } else {
            // Card doesn't exist yet, render all alerts
            renderAlerts();
        }
    }
});

// Fetch portfolio balance
async function fetchPortfolio() {
    try {
        const response = await fetch('/api/portfolio');
        const data = await response.json();
        if (data.balance) {
            portfolioBalance = data.balance;
            // Balance is already in dollars from API, no need to divide by 100
            // HTML already has $ prefix, so just set the number
            balanceElement.textContent = portfolioBalance.toFixed(2);
        }
    } catch (error) {
        console.error('Error fetching portfolio:', error);
    }
}

// Render alerts - auto-sorted by EV (highest first)
function renderAlerts() {
    if (alerts.size === 0) {
        alertsList.innerHTML = '<div class="empty-state"><p>No alerts yet. Waiting for new opportunities...</p></div>';
        return;
    }
    
    alertsList.innerHTML = '';
    
    // Sort alerts by EV (highest first)
    const sortedAlerts = Array.from(alerts.values()).sort((a, b) => (b.ev_percent || 0) - (a.ev_percent || 0));
    
    sortedAlerts.forEach(alert => {
        const alertCard = createAlertCard(alert);
        alertsList.appendChild(alertCard);
    });
}

// Book logo mapping (for display) - maps book names to logo file paths
const bookLogos = {
    'Polymarket': '/logos/poly.png',
    'Kalshi': '/logos/Kalshi.png',  // Keep for display books comparison
    'Pinnacle': '/logos/Pinnacle.png',
    'SportTrade': '/logos/Sporttrade.png',
    'Novig': '/logos/NV.png',
    'ProphetX': '/logos/PX.png',
    'BookMaker': '/logos/BM.png',
    'FanDuel': '/logos/FD.png',
    'DraftKings': '/logos/DK.png',
    'Circa': '/logos/Circa.png'
};

// Create alert card element (BookieBeats style)
function createAlertCard(alert) {
    const card = document.createElement('div');
    // Add high-ev class for alerts with EV >= 8%
    const evClass = (alert.ev_percent || 0) >= 8.0 ? 'high-ev' : '';
    card.className = `alert-card new ${evClass}`;
    card.setAttribute('data-alert-id', alert.id);
    
    const timestamp = new Date(alert.timestamp);
    const timeStr = timestamp.toLocaleTimeString();
    
    // Get submarket name (pick + qualifier)
    // For moneylines, show "ML" instead of "0.0"
    let qualifierDisplay = alert.qualifier;
    if (alert.market_type && alert.market_type.toLowerCase() === 'moneyline' && (qualifierDisplay === '0.0' || qualifierDisplay === '0' || !qualifierDisplay)) {
        qualifierDisplay = 'ML';
    }
    const submarketName = qualifierDisplay ? `${alert.pick} ${qualifierDisplay}`.trim() : alert.pick;
    
    // Format EV with sign
    const evDisplay = `${alert.ev_percent >= 0 ? '+' : ''}${alert.ev_percent.toFixed(2)}%`;
    
    // Get Polymarket price (our betting book) - use display_books if available, otherwise fallback
    const polymarketLiquidity = alert.liquidity ? `$${(alert.liquidity / 1000).toFixed(1)}k` : '$0';
    
    // Build book prices table if display_books data is available
    let booksTableHtml = '';
    let polymarketOddsNum = null; // Will be set from display_books if available
    
    if (alert.display_books && Object.keys(alert.display_books).length > 0) {
        // Get the selection we're betting on
        const ourSelection = alert.pick;
        const ourBooks = alert.display_books[ourSelection] || [];
        
        // Get sharp books from filter (books used for EV calculation)
        const sharpBooks = alert.sharp_books || [];
        const sharpBookNames = new Set(sharpBooks);
        
        // Find Polymarket book first to get actual Polymarket odds for comparison
        const polymarketBook = ourBooks.find(book => (book.book || 'Unknown') === 'Polymarket');
        if (polymarketBook && polymarketBook.odds) {
            polymarketOddsNum = polymarketBook.odds;
        } else {
            // Fallback to alert.book_price if Polymarket not in display_books
            const polymarketPrice = alert.book_price || alert.american_odds || (alert.price_cents ? priceToAmericanOdds(alert.price_cents) : 'N/A');
            if (polymarketPrice && polymarketPrice !== 'N/A') {
                const polymarketStr = String(polymarketPrice).replace(/[+]/g, '');
                polymarketOddsNum = parseInt(polymarketStr, 10);
                if (isNaN(polymarketOddsNum)) {
                    polymarketOddsNum = null;
                }
            }
        }
        
        // Only show: Polymarket + sharp books from filter
        const booksToShow = ourBooks.filter(book => {
            const bookName = book.book || 'Unknown';
            return bookName === 'Polymarket' || sharpBookNames.has(bookName);
        });
        
        if (booksToShow.length > 0) {
            booksTableHtml = '<div class="books-table"><div class="books-header">';
            booksToShow.forEach(book => {
                const bookName = book.book || 'Unknown';
                const bookOdds = book.odds || 0;
                const bookLimit = book.limit || 0;
                const bookLimitDisplay = bookLimit ? `$${(bookLimit / 1000).toFixed(1)}k` : '';
                const bookLogoPath = bookLogos[bookName];
                const bookLogoText = bookName.substring(0, 2).toUpperCase();  // Fallback text if no logo
                const isPolymarket = bookName === 'Polymarket';
                const isSharpBook = sharpBookNames.has(bookName);
                
                // Gray out if not Polymarket and not a sharp book (shouldn't happen due to filter, but safety check)
                let isGrayedOut = !isPolymarket && !isSharpBook;
                
                // Check if this book has BETTER odds than Polymarket (for red outline)
                // Red box = book is BETTER than Polymarket (we're missing out on a better price)
                let hasBetterOdds = false;
                if (!isPolymarket && polymarketOddsNum !== null && bookOdds !== 0) {
                    // Compare odds: better = higher for positive, less negative for negative
                    if (bookOdds > 0 && polymarketOddsNum > 0) {
                        // Both positive: higher is better
                        hasBetterOdds = bookOdds > polymarketOddsNum;
                    } else if (bookOdds < 0 && polymarketOddsNum < 0) {
                        // Both negative: less negative is better (closer to 0)
                        hasBetterOdds = bookOdds > polymarketOddsNum; // e.g., -105 > -112
                    } else if (bookOdds > 0 && polymarketOddsNum < 0) {
                        // Book is positive, Polymarket is negative: book is better
                        hasBetterOdds = true;
                    } else if (bookOdds < 0 && polymarketOddsNum > 0) {
                        // Book is negative, Polymarket is positive: Polymarket is better (but we bet Polymarket, so don't mark)
                        hasBetterOdds = false;
                    }
                }
                
                // Use image if logo path exists, otherwise use text
                const logoHtml = bookLogoPath 
                    ? `<img src="${bookLogoPath}" alt="${bookName}" class="book-logo-img" />`
                    : `<div class="book-logo-text">${bookLogoText}</div>`;
                
                booksTableHtml += `
                    <div class="book-cell ${isPolymarket ? 'polymarket-book' : ''} ${isGrayedOut ? 'grayed-out' : ''} ${hasBetterOdds ? 'better-than-polymarket' : ''}" data-book-name="${escapeHtml(bookName)}">
                        <div class="book-logo">${logoHtml}</div>
                        <div class="book-odds" data-book-odds="${bookOdds}">${bookOdds > 0 ? '+' : ''}${bookOdds}</div>
                        ${bookLimitDisplay ? `<div class="book-limit" data-book-limit="${bookLimit}">${bookLimitDisplay}</div>` : ''}
                    </div>
                `;
            });
            booksTableHtml += '</div></div>';
        }
    }
    
    // Determine which books meet filter criteria (for graying out)
    // Books are grayed out if they don't meet minSharpLimits or other filter criteria
    const devigBooks = alert.devig_books || [];  // Books used for devigging (from API)
    const minSharpLimits = {
        'BookMaker': 250,
        'Circa': 250,
        'Novig': 200,
        'Pinnacle': 250,
        'ProphetX': 200,
        'SportTrade': 200,
        'DraftKings': 250,
        'FanDuel': 250
    };
    
    // Get filter name (if available)
    const filterName = alert.filter_name || '';
    const filterNameDisplay = filterName ? `<div class="filter-name-badge" style="position: absolute; top: 8px; right: 8px; background: rgba(0, 102, 255, 0.2); color: #0066FF; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; border: 1px solid rgba(0, 102, 255, 0.3);">${escapeHtml(filterName)}</div>` : '';
    
    card.innerHTML = `
        <div class="alert-header-bb" style="position: relative;">
            ${filterNameDisplay}
            <div class="alert-main-info">
                <div class="market-type-bb">${escapeHtml(alert.market_type)}</div>
                <div class="teams-bb">${escapeHtml(alert.teams)}</div>
                <div class="ev-display-left">
                    <div class="ev-value ${alert.ev_percent >= 0 ? 'positive' : 'negative'}">${evDisplay}</div>
                    <div class="ev-team">${escapeHtml(submarketName)}</div>
                </div>
            </div>
        </div>
        
        ${booksTableHtml}
        
        <div class="alert-betting-section">
            <div class="bet-actions-bb">
                <div class="bet-input-group-bb">
                    <input 
                        type="number" 
                        class="bet-input-bb" 
                        placeholder="Amount ($)"
                        min="0"
                        step="0.01"
                        data-alert-id="${alert.id}"
                    >
                    <button 
                        class="btn btn-bet-bb"
                        onclick="placeBet('${alert.id}', false)"
                    >
                        BET
                    </button>
                </div>
                ${alert.liquidity > 0 ? `
                    <button 
                        class="btn btn-bet-max-bb"
                        onclick="placeBet('${alert.id}', true)"
                    >
                        BET MAX ($${Math.min(maxBetAmount, alert.liquidity).toFixed(0)})
                    </button>
                ` : ''}
            </div>
        </div>
        
        <div class="bet-status"></div>
        <div class="timestamp-bb">${timeStr}</div>
    `;
    
    // Add Enter key handler for bet input
    const input = card.querySelector('.bet-input-bb');
    if (input) {
        input.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                placeBet(alert.id, false);
            }
        });
    }
    
    return card;
}

// Remove alert manually
async function removeAlert(alertId) {
    if (!confirm('Remove this alert from the dashboard?')) {
        return;
    }
    
    try {
        const response = await fetch('/api/remove_alert', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ alert_id: alertId })
        });
        
        const result = await response.json();
        if (result.success) {
            console.log(`Removed alert ${alertId}`);
            // The server will emit 'remove_alert' which will trigger the handler
        } else {
            console.error('Failed to remove alert:', result.error);
            alert('Failed to remove alert: ' + (result.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('Error removing alert:', error);
        alert('Error removing alert: ' + error.message);
    }
}

// Place bet
async function placeBet(alertId, betMax) {
    const alert = alerts.get(String(alertId));
    if (!alert) {
        console.error('Alert not found:', alertId);
        return;
    }
    
    const alertCard = document.querySelector(`[data-alert-id="${alertId}"]`);
    if (!alertCard) return;
    
    const statusDiv = alertCard.querySelector('.bet-status');
    statusDiv.className = 'bet-status pending';
    statusDiv.textContent = '⏳ Placing bet...';
    
    let betAmount = 0;
    if (!betMax) {
        // Try both class names (legacy .bet-input and new .bet-input-bb)
        const input = alertCard.querySelector('.bet-input-bb') || alertCard.querySelector('.bet-input');
        if (!input) {
            statusDiv.className = 'bet-status error';
            statusDiv.textContent = '❌ Bet input field not found';
            console.error('Could not find bet input field in alert card');
            return;
        }
        betAmount = parseFloat(input.value) || 0;
        if (betAmount <= 0) {
            statusDiv.className = 'bet-status error';
            statusDiv.textContent = '❌ Please enter a valid bet amount';
            return;
        }
    }
    
    try {
        const response = await fetch('/api/place_bet', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                alert_id: alertId,
                bet_amount: betAmount,
                bet_max: betMax
            })
        });
        
        const result = await response.json();
        
        if (result.success) {
            statusDiv.className = 'bet-status success';
            statusDiv.textContent = `✅ Bet placed! ${result.count} contracts at ${(result.price_cents / 100).toFixed(2)}¢`;
            
            // Clear input (try both class names)
            const input = alertCard.querySelector('.bet-input-bb') || alertCard.querySelector('.bet-input');
            if (input) input.value = '';
            
            // Refresh portfolio
            fetchPortfolio();
        } else {
            statusDiv.className = 'bet-status error';
            statusDiv.textContent = `❌ Error: ${result.error || 'Unknown error'}`;
        }
    } catch (error) {
        console.error('Error placing bet:', error);
        statusDiv.className = 'bet-status error';
        statusDiv.textContent = `❌ Network error: ${error.message}`;
    }
}

// Refresh button
refreshBtn.addEventListener('click', () => {
    fetchPortfolio();
    // Request alerts update
    socket.emit('request_alerts');
});

// Utility function
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Max bet amount setting
async function fetchMaxBet() {
    try {
        const response = await fetch('/api/get_max_bet');
        const data = await response.json();
        if (data.max_bet_amount) {
            maxBetAmount = data.max_bet_amount;
            maxBetInput.value = maxBetAmount;
        }
    } catch (error) {
        console.error('Error fetching max bet:', error);
    }
}

async function setMaxBet() {
    const amount = parseFloat(maxBetInput.value);
    if (isNaN(amount) || amount <= 0) {
        showToast('error', 'Invalid max bet amount', null);
        return;
    }
    
    try {
        const response = await fetch('/api/set_max_bet', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ max_amount: amount })
        });
        
        const data = await response.json();
        if (data.success) {
            maxBetAmount = data.max_bet_amount;
            showToast('success', `Max bet set to $${maxBetAmount}`, null);
        } else {
            showToast('error', data.error || 'Failed to set max bet', null);
        }
    } catch (error) {
        console.error('Error setting max bet:', error);
        showToast('error', 'Failed to set max bet', null);
    }
}

setMaxBetBtn.addEventListener('click', setMaxBet);
maxBetInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        setMaxBet();
    }
});

// Toast notification system
function showToast(status, message, details) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    
    const toast = document.createElement('div');
    toast.className = `toast ${status}`;
    
    const icon = status === 'success' ? '✅' : '❌';
    const detailsText = details ? 
        (details.count ? `${details.count} contracts @ ${(details.price_cents / 100).toFixed(2)}¢` : '') : '';
    
    toast.innerHTML = `
        <span class="toast-icon">${icon}</span>
        <div class="toast-content">
            <div class="toast-message">${escapeHtml(message)}</div>
            ${detailsText ? `<div class="toast-details">${escapeHtml(detailsText)}</div>` : ''}
        </div>
        <button class="toast-close" onclick="this.parentElement.remove()">×</button>
    `;
    
    container.appendChild(toast);
    
    // Auto-remove after 5 seconds
    setTimeout(() => {
        toast.classList.add('hiding');
        setTimeout(() => {
            if (toast.parentElement) {
                toast.remove();
            }
        }, 300);
    }, 5000);
}

// Bet success popup with cost, odds, and win amount
function showBetSuccessPopup(data) {
    // Create modal overlay
    const overlay = document.createElement('div');
    overlay.className = 'bet-success-overlay';
    overlay.style.cssText = `
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background: rgba(0, 0, 0, 0.7);
        display: flex;
        justify-content: center;
        align-items: center;
        z-index: 10000;
        animation: fadeIn 0.2s;
    `;
    
    // Create popup
    const popup = document.createElement('div');
    popup.className = 'bet-success-popup';
    popup.style.cssText = `
        background: #1a1a1a;
        border: 2px solid #0066FF;
        border-radius: 12px;
        padding: 30px;
        max-width: 400px;
        width: 90%;
        box-shadow: 0 8px 32px rgba(0, 102, 255, 0.3);
        animation: slideUp 0.3s;
    `;
    
    const cost = data.cost || 0;
    const americanOdds = data.american_odds || 'N/A';
    const winAmount = data.win_amount || 0;
    const fillCount = data.fill_count || 0;
    const status = data.status || 'filled';
    const statusText = status === 'filled' || status === 'executed' ? 'Fully Filled' : 'Partially Filled';
    
    // Get pick and qualifier (e.g., "Under 152.5")
    const pick = data.pick || '';
    const qualifier = data.qualifier || '';
    const pickWithQualifier = qualifier ? `${pick} ${qualifier}`.trim() : pick;
    
    // Get actual executed price in cents (from Polymarket)
    const executedPriceCents = data.executed_price_cents || data.price_cents || 0;
    const executedPriceDisplay = executedPriceCents > 0 ? `${executedPriceCents}¢` : 'N/A';
    
    // Get market info
    const marketName = data.market_name || data.teams || 'N/A';
    const ticker = data.ticker || 'N/A';
    const filterName = data.filter_name || '';
    
    // Check if this is an auto-bet (has ev_percent field)
    const isAutoBet = data.ev_percent !== undefined;
    const titleText = isAutoBet ? '🚀 Auto-Bet Placed!' : 'Bet Placed Successfully!';
    const evText = isAutoBet ? ` • ${data.ev_percent.toFixed(2)}% EV` : '';
    const filterNameDisplay = filterName ? `<div style="margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px solid #333;">
                <div style="color: #aaa; font-size: 12px; margin-bottom: 5px;">Filter:</div>
                <div style="color: #0066FF; font-size: 14px; font-weight: 500;">${escapeHtml(filterName)}</div>
            </div>` : '';
    
    popup.innerHTML = `
        <div style="text-align: center; margin-bottom: 20px;">
            <div style="font-size: 48px; margin-bottom: 10px;">${isAutoBet ? '🚀' : '✅'}</div>
            <h2 style="color: #0066FF; margin: 0; font-size: 24px;">${titleText}</h2>
            <p style="color: #888; margin: 5px 0 0 0; font-size: 14px;">${statusText} • ${fillCount} contracts${evText}</p>
        </div>
        
        <div style="background: #0a0a0a; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
            ${filterNameDisplay}
            <div style="margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px solid #333;">
                <div style="color: #aaa; font-size: 12px; margin-bottom: 5px;">Market:</div>
                <div style="color: #fff; font-size: 14px; font-weight: 500;">${escapeHtml(marketName)}</div>
            </div>
            <div style="margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px solid #333;">
                <div style="color: #aaa; font-size: 12px; margin-bottom: 5px;">Pick:</div>
                <div style="color: #fff; font-size: 16px; font-weight: bold;">${escapeHtml(pickWithQualifier || 'N/A')}</div>
            </div>
            <div style="margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px solid #333;">
                <div style="color: #aaa; font-size: 12px; margin-bottom: 5px;">Ticker:</div>
                <div style="color: #888; font-size: 12px; font-family: monospace;">${escapeHtml(ticker)}</div>
            </div>
            <div style="display: flex; justify-content: space-between; margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px solid #333;">
                <span style="color: #aaa; font-size: 14px;">Actual Price:</span>
                <span style="color: #0066FF; font-size: 18px; font-weight: bold;">${executedPriceDisplay}</span>
            </div>
            <div style="display: flex; justify-content: space-between; margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px solid #333;">
                <span style="color: #aaa; font-size: 14px;">American Odds:</span>
                <span style="color: #0066FF; font-size: 18px; font-weight: bold;">${americanOdds}</span>
            </div>
            <div style="display: flex; justify-content: space-between; margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px solid #333;">
                <span style="color: #aaa; font-size: 14px;">Amount Bet:</span>
                <span style="color: #fff; font-size: 18px; font-weight: bold;">$${cost.toFixed(2)}</span>
            </div>
            <div style="display: flex; justify-content: space-between;">
                <span style="color: #aaa; font-size: 14px;">Win Amount:</span>
                <span style="color: #0066FF; font-size: 20px; font-weight: bold;">+$${winAmount.toFixed(2)}</span>
            </div>
        </div>
        
        <button onclick="this.closest('.bet-success-overlay').remove()" 
                style="width: 100%; padding: 12px; background: #0066FF; color: #fff; border: none; border-radius: 6px; font-size: 16px; font-weight: bold; cursor: pointer; transition: background 0.2s;"
                onmouseover="this.style.background='#0052CC'"
                onmouseout="this.style.background='#0066FF'">
            Close
        </button>
    `;
    
    overlay.appendChild(popup);
    document.body.appendChild(overlay);
    
    // Close on overlay click
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) {
            overlay.remove();
        }
    });
    
    // NO AUTO-CLOSE - user must click Close button
}

// Add CSS animations
if (!document.getElementById('bet-success-styles')) {
    const style = document.createElement('style');
    style.id = 'bet-success-styles';
    style.textContent = `
        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }
        @keyframes fadeOut {
            from { opacity: 1; }
            to { opacity: 0; }
        }
        @keyframes slideUp {
            from { transform: translateY(20px); opacity: 0; }
            to { transform: translateY(0); opacity: 1; }
        }
    `;
    document.head.appendChild(style);
}

// Filter management
async function loadFilterSettings() {
    try {
        const response = await fetch('/api/get_filters');
        const data = await response.json();
        
        // Populate filter checkboxes
        if (data.saved_filters) {
            // Clear existing checkboxes
            dashboardFiltersCheckboxes.innerHTML = '';
            // Add all saved filters as checkboxes (only for dashboard - auto-bettor is managed in auto-bet settings)
            for (const filterName in data.saved_filters) {
                // Dashboard filter checkbox
                const checkboxItem1 = document.createElement('div');
                checkboxItem1.className = 'filter-checkbox-item';
                const checkbox1 = document.createElement('input');
                checkbox1.type = 'checkbox';
                checkbox1.id = `dashboard-filter-${filterName}`;
                checkbox1.value = filterName;
                checkbox1.checked = data.selected_dashboard_filters && data.selected_dashboard_filters.includes(filterName);
                const label1 = document.createElement('label');
                label1.htmlFor = `dashboard-filter-${filterName}`;
                label1.textContent = filterName;
                checkboxItem1.appendChild(checkbox1);
                checkboxItem1.appendChild(label1);
                // Make entire item clickable
                checkboxItem1.addEventListener('click', (e) => {
                    if (e.target !== checkbox1 && e.target !== label1) {
                        checkbox1.checked = !checkbox1.checked;
                    }
                });
                dashboardFiltersCheckboxes.appendChild(checkboxItem1);
                
                // Auto-bettor filters are now managed in the auto-bet settings section below
                // No need to create checkboxes here
            }
        }
        
        // Load dashboard min EV and max bet from backend
        if (data.dashboard_min_ev !== undefined) {
            minEvInput.value = data.dashboard_min_ev;
        }
        if (data.max_bet_amount !== undefined) {
            maxBetInput.value = data.max_bet_amount;
            maxBetAmount = data.max_bet_amount;
        }
    } catch (error) {
        console.error('Error loading filter settings:', error);
    }
}

async function saveFilterSelection() {
    const selectedDashboard = Array.from(dashboardFiltersCheckboxes.querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value);
    
    // Auto-bettor filters are managed in the auto-bet settings section below
    // Get current auto-bettor selection from auto-bet settings checkboxes
    const autoBettorCheckboxes = document.querySelectorAll('.filter-auto-bet-checkbox:checked');
    const selectedAutoBettor = Array.from(autoBettorCheckboxes).map(cb => cb.getAttribute('data-filter'));
    
    // Allow 0 selections - user can disable all filters if needed
    // No validation needed - backend will handle empty arrays
    
    try {
        const response = await fetch('/api/set_selected_filters', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                dashboard_filters: selectedDashboard,
                auto_bettor_filters: selectedAutoBettor
            })
        });
        
        const data = await response.json();
        if (data.success) {
            const dashboardText = selectedDashboard.length === 0 ? 'None' : `${selectedDashboard.length} filter(s)`;
            showToast('success', `Dashboard filters saved: ${dashboardText}`, null);
            // Note: Auto-bettor filters are saved separately when auto-bet settings are saved
        } else {
            showToast('error', data.error || 'Failed to save filter selection', null);
        }
    } catch (error) {
        console.error('Error saving filter selection:', error);
        showToast('error', 'Failed to save filter selection', null);
    }
}

async function fetchFilters() {
    try {
        const response = await fetch('/api/get_filters');
        const data = await response.json();
        if (data.filters) {
            const minEv = data.filters.devigFilter?.minEv || 3;
            minEvInput.value = minEv;
        }
    } catch (error) {
        console.error('Error fetching filters:', error);
    }
}

async function setFilters() {
    const minEv = parseFloat(minEvInput.value);
    if (isNaN(minEv) || minEv < 0) {
        showToast('error', 'Invalid min EV value', null);
        return;
    }
    
    try {
        const response = await fetch('/api/set_filters', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                min_ev: minEv
            })
        });
        
        const data = await response.json();
        if (data.success) {
            showToast('success', `Dashboard Min EV updated: ${minEv}%`, null);
        } else {
            showToast('error', data.error || 'Failed to update filters', null);
        }
    } catch (error) {
        console.error('Error setting filters:', error);
        showToast('error', 'Failed to update filters', null);
    }
}

setFiltersBtn.addEventListener('click', setFilters);
minEvInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') {
        setFilters();
    }
});

// Filter selection event listeners
if (saveFilterSelectionBtn) {
    saveFilterSelectionBtn.addEventListener('click', saveFilterSelection);
}

// Auto-bet functions
async function loadAutoBetSettings() {
    try {
        const response = await fetch('/api/get_auto_bet');
        const data = await response.json();
        
        autoBetEnabled.checked = data.enabled || false;
        
        // Load NHL overs amount (shared)
        if (autoBetNhlOversAmount) {
            autoBetNhlOversAmount.value = data.nhl_over_amount || data.nhl_overs_amount || 200.0;
        }
        
        // Render per-filter auto-bet settings
        const perFilterContainer = document.getElementById('per-filter-auto-bet-settings');
        if (perFilterContainer && data.settings_by_filter && data.available_filters) {
            perFilterContainer.innerHTML = '';
            // Container styling is handled by CSS (supports dark mode)
            
            // Create a section for each filter
            for (const filterName of data.available_filters) {
                const filterSettings = data.settings_by_filter[filterName] || {
                    ev_min: 5.0,
                    ev_max: 25.0,
                    odds_min: -200,
                    odds_max: 200,
                    amount: 101.0,
                    enabled: true
                };
                const isSelected = data.selected_auto_bettor_filters && data.selected_auto_bettor_filters.includes(filterName);
                
                const filterSection = document.createElement('div');
                filterSection.className = 'filter-auto-bet-section';
                
                filterSection.innerHTML = `
                    <div style="display: flex; align-items: center; margin-bottom: 10px;">
                        <label style="display: flex; align-items: center; font-weight: 600; font-size: 14px;">
                            <input type="checkbox" class="filter-auto-bet-checkbox" data-filter="${escapeHtml(filterName)}" ${isSelected ? 'checked' : ''} style="margin-right: 8px;">
                            <span>${escapeHtml(filterName)}</span>
                        </label>
                    </div>
                    <div class="auto-bet-row">
                        <label>EV Range: </label>
                        <input type="number" class="auto-bet-input filter-ev-min" data-filter="${escapeHtml(filterName)}" value="${filterSettings.ev_min || 5.0}" min="0" max="100" step="0.1" placeholder="Min">
                        <span>% - </span>
                        <input type="number" class="auto-bet-input filter-ev-max" data-filter="${escapeHtml(filterName)}" value="${filterSettings.ev_max || 25.0}" min="0" max="100" step="0.1" placeholder="Max">
                        <span>%</span>
                    </div>
                    <div class="auto-bet-row">
                        <label>Odds Range: </label>
                        <input type="number" class="auto-bet-input filter-odds-min" data-filter="${escapeHtml(filterName)}" value="${filterSettings.odds_min || -200}" step="1" placeholder="Min">
                        <span> to </span>
                        <input type="number" class="auto-bet-input filter-odds-max" data-filter="${escapeHtml(filterName)}" value="${filterSettings.odds_max || 200}" step="1" placeholder="Max">
                    </div>
                    <div class="auto-bet-row">
                        <label>Bet Amount: $</label>
                        <input type="number" class="auto-bet-input filter-amount" data-filter="${escapeHtml(filterName)}" value="${filterSettings.amount || 101.0}" min="1" step="1">
                    </div>
                `;
                
                perFilterContainer.appendChild(filterSection);
            }
        }
        
        // Legacy support - if no per-filter settings, use global
        if (!data.settings_by_filter) {
            const autoBetConfig = document.getElementById('auto-bet-config');
            if (autoBetConfig && !perFilterContainer) {
                // Fallback to old UI if per-filter container doesn't exist
                const evMinInput = document.getElementById('auto-bet-ev-min');
                const evMaxInput = document.getElementById('auto-bet-ev-max');
                const oddsMinInput = document.getElementById('auto-bet-odds-min');
                const oddsMaxInput = document.getElementById('auto-bet-odds-max');
                const amountInput = document.getElementById('auto-bet-amount');
                
                if (evMinInput) evMinInput.value = data.ev_min || 8.0;
                if (evMaxInput) evMaxInput.value = data.ev_max || 20.0;
                if (oddsMinInput) oddsMinInput.value = data.odds_min || -150;
                if (oddsMaxInput) oddsMaxInput.value = data.odds_max || 150;
                if (amountInput) amountInput.value = data.amount || 100.0;
            }
        }
    } catch (error) {
        console.error('Error loading auto-bet settings:', error);
    }
}

// Debounce function to limit API calls
let saveTimeout = null;
function debouncedSave() {
    if (saveTimeout) {
        clearTimeout(saveTimeout);
    }
    saveTimeout = setTimeout(() => {
        saveAutoBetSettings(true); // true = silent save (no toast)
    }, 500); // Wait 500ms after last change
}

async function saveAutoBetSettings(quiet = false) {
    try {
        // Collect per-filter settings
        const settingsByFilter = {};
        const selectedAutoBettorFilters = [];
        
        const filterCheckboxes = document.querySelectorAll('.filter-auto-bet-checkbox');
        filterCheckboxes.forEach(checkbox => {
            const filterName = checkbox.getAttribute('data-filter');
            const evMinInput = document.querySelector(`.filter-ev-min[data-filter="${filterName}"]`);
            const evMaxInput = document.querySelector(`.filter-ev-max[data-filter="${filterName}"]`);
            const oddsMinInput = document.querySelector(`.filter-odds-min[data-filter="${filterName}"]`);
            const oddsMaxInput = document.querySelector(`.filter-odds-max[data-filter="${filterName}"]`);
            const amountInput = document.querySelector(`.filter-amount[data-filter="${filterName}"]`);
            
            if (evMinInput && evMaxInput && oddsMinInput && oddsMaxInput && amountInput) {
                settingsByFilter[filterName] = {
                    ev_min: parseFloat(evMinInput.value) || 5.0,
                    ev_max: parseFloat(evMaxInput.value) || 25.0,
                    odds_min: parseInt(oddsMinInput.value) || -200,
                    odds_max: parseInt(oddsMaxInput.value) || 200,
                    amount: parseFloat(amountInput.value) || 101.0,
                    enabled: checkbox.checked
                };
                
                if (checkbox.checked) {
                    selectedAutoBettorFilters.push(filterName);
                }
            }
        });
        
        const response = await fetch('/api/set_auto_bet', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                enabled: autoBetEnabled.checked,
                nhl_over_amount: autoBetNhlOversAmount ? parseFloat(autoBetNhlOversAmount.value) : 200.0,
                settings_by_filter: settingsByFilter,
                selected_auto_bettor_filters: selectedAutoBettorFilters,
                // Legacy fields for backward compatibility (if old UI elements exist)
                ev_min: autoBetEvMin ? parseFloat(autoBetEvMin.value) : undefined,
                ev_max: autoBetEvMax ? parseFloat(autoBetEvMax.value) : undefined,
                odds_min: autoBetOddsMin ? parseInt(autoBetOddsMin.value) : undefined,
                odds_max: autoBetOddsMax ? parseInt(autoBetOddsMax.value) : undefined,
                amount: autoBetAmount ? parseFloat(autoBetAmount.value) : undefined
            })
        });
        
        const data = await response.json();
        if (data.success) {
            if (!quiet) {
                showToast('success', `Auto-bet settings saved`, null);
            }
            console.log('Auto-bet settings saved:', data);
        } else {
            if (!quiet) {
                showToast('error', 'Failed to save auto-bet settings', null);
            }
        }
    } catch (error) {
        console.error('Error saving auto-bet settings:', error);
        if (!quiet) {
            showToast('error', 'Failed to save auto-bet settings', null);
        }
    }
}

// Auto-bet event listeners
autoBetEnabled.addEventListener('change', () => {
    // Config is always visible - just save settings when toggle changes
    saveAutoBetSettings();
});

// Auto-save on input change for all fields (debounced) - legacy support
if (autoBetEvMin) autoBetEvMin.addEventListener('input', debouncedSave);
if (autoBetEvMax) autoBetEvMax.addEventListener('input', debouncedSave);
if (autoBetOddsMin) autoBetOddsMin.addEventListener('input', debouncedSave);
if (autoBetOddsMax) autoBetOddsMax.addEventListener('input', debouncedSave);
if (autoBetAmount) autoBetAmount.addEventListener('input', debouncedSave);
if (autoBetNhlOversAmount) {
    autoBetNhlOversAmount.addEventListener('input', debouncedSave);
}

// Add event listeners for per-filter settings (delegated event handling)
document.addEventListener('input', (e) => {
    if (e.target.classList.contains('filter-ev-min') || 
        e.target.classList.contains('filter-ev-max') ||
        e.target.classList.contains('filter-odds-min') ||
        e.target.classList.contains('filter-odds-max') ||
        e.target.classList.contains('filter-amount')) {
        debouncedSave();
    }
});

document.addEventListener('change', (e) => {
    if (e.target.classList.contains('filter-auto-bet-checkbox')) {
        saveAutoBetSettings();
    }
});

// Save button still works for immediate save
if (saveAutoBetBtn) {
    saveAutoBetBtn.addEventListener('click', () => {
        if (saveTimeout) {
            clearTimeout(saveTimeout);
        }
        saveAutoBetSettings();
    });
}

// Token update functionality
const showTokenBtn = document.getElementById('show-token-btn');
const tokenSetting = document.getElementById('token-setting');
const tokenInput = document.getElementById('token-input');
const updateTokenBtn = document.getElementById('update-token-btn');
const toggleTokenBtn = document.getElementById('toggle-token-btn');

let tokenVisible = false;

if (showTokenBtn) {
    showTokenBtn.addEventListener('click', () => {
        tokenSetting.style.display = tokenSetting.style.display === 'none' ? 'block' : 'none';
    });
}

if (toggleTokenBtn) {
    toggleTokenBtn.addEventListener('click', () => {
        tokenVisible = !tokenVisible;
        tokenInput.type = tokenVisible ? 'text' : 'password';
        toggleTokenBtn.textContent = tokenVisible ? 'Hide' : 'Show';
    });
}

if (updateTokenBtn) {
    updateTokenBtn.addEventListener('click', async () => {
        const token = tokenInput.value.trim();
        if (!token) {
            showToast('error', 'Please enter a token', null);
            return;
        }
        
        updateTokenBtn.disabled = true;
        updateTokenBtn.textContent = 'Updating...';
        
        try {
            const response = await fetch('/api/update_token', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ token: token })
            });
            
            const data = await response.json();
            if (data.success) {
                showToast('success', 'Token updated successfully!', null);
                tokenInput.value = '';
                tokenSetting.style.display = 'none';
            } else {
                showToast('error', data.error || 'Failed to update token', null);
            }
        } catch (error) {
            console.error('Error updating token:', error);
            showToast('error', 'Failed to update token', null);
        } finally {
            updateTokenBtn.disabled = false;
            updateTokenBtn.textContent = 'Update';
        }
    });
    
    if (tokenInput) {
        tokenInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                updateTokenBtn.click();
            }
        });
    }
}

// Initialize
fetchPortfolio();
fetchMaxBet();
fetchFilters();


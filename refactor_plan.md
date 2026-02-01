# 2v2 Match Support - Code Review & Refactor Plan

**Branch:** `support_doubles`
**Date:** 2026-01-29
**Reviewer:** Senior Django Lead

---

## Summary of Changes Detected

The codebase has been migrated from a 1v1-only table tennis tracking system to support both 1v1 and 2v2 (doubles) matches. Key architectural changes:

1. **New Team Model:** Introduced `Team` model with ManyToMany relationship to `Player`
2. **Match Model Refactor:**
   - Replaced `player1`/`player2` ForeignKeys with `team1`/`team2` ForeignKeys
   - Added `is_double` boolean flag
   - Changed `winner` from Player FK to Team FK
   - Replaced boolean confirmation fields with ManyToMany `confirmations` through `MatchConfirmation` model
3. **Game Model Updates:** Renamed `player1_score`/`player2_score` to `team1_score`/`team2_score`, changed `winner` from Player to Team
4. **ScheduledMatch Updates:** Migrated from `player1`/`player2` to `team1`/`team2`
5. **Migrations:** 4 new migrations (0008-0013) handle data migration from old structure to new

**Migration Strategy:** The migrations use data migration functions to create Team objects from existing player relationships and preserve historical data.

---

## UI/UX Strategy and Form Architecture

### User Experience Goals

**Match Creation Flow:**
1. User toggles between singles (1v1) and doubles (2v2) using `is_double` dropdown
2. User selects individual players from dropdowns (not pre-existing teams)
3. For singles: Select 2 players
4. For doubles: Select 4 players (2 per team)
5. Teams are created programmatically from player selections in the view's `form_valid()` method

**Player Selection Rules:**
1. **Non-staff users:**
   - ALWAYS locked as Player 1 (pre-filled, disabled field)
   - Cannot change Player 1 selection
   - Can only select Player 2 (singles) or Players 2, 3, 4 (doubles)
2. **Staff users:**
   - Can select any players for all positions
   - No restrictions

**Dynamic Dropdown Behavior (REQUIRED):**
- As players are selected, other dropdowns should dynamically exclude already-selected players
- Example: If user selects "Alice" as Player 2, then Player 3 and Player 4 dropdowns should not show "Alice"
- This prevents duplicate player selections and improves UX
- **Implementation:** Requires JavaScript to update dropdown options in real-time

### Form Architecture

**CRITICAL: Forms use player1-4 fields, NOT team1/team2**

```
MatchForm:
  - Exposes: player1, player2, player3, player4 (ModelChoiceField instances)
  - Does NOT expose: team1, team2 (these are model fields but NOT form fields)
  - player3 and player4 are optional (required=False) for singles matches

ScheduledMatchForm:
  - Exposes: player1, player2 (ModelChoiceField instances)
  - Does NOT expose: team1, team2
  - Singles matches only (no is_double field)
```

**Team Creation Workflow:**
1. User submits form with player selections (player1, player2, optionally player3/player4)
2. Form validation ensures all selected players are unique
3. View's `form_valid()` method creates Team objects from selected players:
   - Singles: Create 2 teams with 1 player each
   - Doubles: Create 2 teams with 2 players each
4. View assigns created teams to `match.team1` and `match.team2`
5. Match is saved with team relationships

**Why this approach:**
- Supports the UX goal of selecting individual players via dropdowns
- Maintains team-based data model for 2v2 support
- Keeps forms simple (no complex team selection UI)
- Teams are implementation detail, not exposed to users

---

## Critical Issues (Will Break the App)

### 1. **forms.py - Missing Field Declarations** ⚠️ BLOCKING

**Location:** `ttstats/pingpong/forms.py`

**Issues:**
- Line 11: `MatchForm.Meta.fields` includes `['team1', 'team2', ...]` but per the form architecture, **forms should NOT expose team1/team2 fields** - these are created programmatically in the view
- Lines 24-35: Widget definitions for `player1`, `player2`, `player3`, `player4` exist but **these fields are not declared as form fields**
- Lines 54-75: `MatchForm.clean()` validates `player1`, `player2`, `player3`, `player4` which don't exist in `cleaned_data` because fields weren't declared
- Line 147: `ScheduledMatchForm.Meta.fields` includes `['team1', 'team2', ...]` - should use player1/player2
- Lines 157-162: `ScheduledMatchForm` widgets reference `player1`/`player2` that aren't declared
- Lines 176-182: `ScheduledMatchForm.clean()` validates `player1`/`player2` that don't exist

**Why It Breaks:**
- Django will raise `KeyError` when trying to access `cleaned_data['player1']` in `clean()` method
- Form submission will fail because player data isn't captured
- Team creation in view will fail because player data isn't available

**Action Required:**

**For MatchForm:**
1. **Declare player fields explicitly** (outside of Meta.fields, since they're not model fields):
   ```python
   player1 = forms.ModelChoiceField(
       queryset=Player.objects.all(),
       required=True,
       widget=forms.Select(attrs={'class': '...'})
   )
   player2 = forms.ModelChoiceField(
       queryset=Player.objects.all(),
       required=True,
       widget=forms.Select(attrs={'class': '...'})
   )
   player3 = forms.ModelChoiceField(
       queryset=Player.objects.all(),
       required=False,  # Optional for singles
       widget=forms.Select(attrs={'class': '...'})
   )
   player4 = forms.ModelChoiceField(
       queryset=Player.objects.all(),
       required=False,  # Optional for singles
       widget=forms.Select(attrs={'class': '...'})
   )
   ```

2. **Update Meta.fields** to remove team1/team2:
   ```python
   fields = ['is_double', 'date_played', 'location', 'match_type', 'best_of', 'notes']
   ```

3. **Remove player1-4 from widgets dict** (lines 24-35) since they're now declared as fields with widgets

4. **Fix is_double widget** - should be `Select`, not `ChoiceField`:
   ```python
   'is_double': forms.Select(
       choices=[('False', 'Single'), ('True', 'Double')],
       attrs={'class': '...'}
   )
   ```

5. **Update clean() method validation** (already correct, just needs fields to exist):
   - Validates all 4 players are unique (for doubles)
   - Validates player1 != player2 (for singles)
   - Should also validate player3/player4 are None for singles matches

**For ScheduledMatchForm:**
1. **Declare player1 and player2 fields** (same pattern as MatchForm)
2. **Update Meta.fields** to remove team1/team2:
   ```python
   fields = ['scheduled_date', 'scheduled_time', 'location', 'notes']
   ```
3. **Remove player1/player2 from widgets dict** (lines 157-162)
4. **Keep existing clean() validation** (lines 176-182, already correct logic)

---

### 2. **views.py - MatchCreateView Missing Team Creation Logic** ⚠️ BLOCKING

**Location:** `ttstats/pingpong/views.py:291-417`

**Current State Analysis:**
- Lines 326-348: `get_form()` correctly tries to work with player1-4 fields ✓
- Line 326-327: Correctly locks player1 for non-staff users ✓
- Lines 336-348: Correctly limits player2/3/4 querysets to exclude current user ✓
- Lines 367-370: Correctly extracts player1-4 from `cleaned_data` ✓

**Issues:**
- Line 366: `form.is_double = ...` - Sets attribute on form instead of variable
- Lines 372-374: Sets `form.player3 = None` instead of variable
- Line 384: Uses bitwise OR `|` instead of logical `or`
- **MISSING:** Team creation logic! The view extracts players but never creates Team objects
- **MISSING:** Assignment of created teams to `match.team1` and `match.team2`

**Why It Breaks:**
- Match is saved without team1/team2, causing IntegrityError (NOT NULL constraint)
- Player selections are lost because teams aren't created

**Action Required:**

1. **Fix form_valid() to create teams from selected players:**
   ```python
   def form_valid(self, form):
       user = self.request.user
       is_double = (form.cleaned_data.get('is_double') == 'True')
       player1 = form.cleaned_data["player1"]
       player2 = form.cleaned_data["player2"]
       player3 = form.cleaned_data.get("player3")  # Optional
       player4 = form.cleaned_data.get("player4")  # Optional

       # Validation for singles matches
       if not is_double:
           if player3 or player4:
               messages.error(self.request, "Singles matches cannot have Player 3 or Player 4!")
               return self.form_invalid(form)

       # Validation for doubles matches
       if is_double:
           if not player3 or not player4:
               messages.error(self.request, "Doubles matches require all 4 players!")
               return self.form_invalid(form)

           # Ensure all 4 players are unique
           players = [player1, player2, player3, player4]
           if len(set(players)) != 4:
               messages.error(self.request, "All players must be different!")
               return self.form_invalid(form)

       # Ensure player1 != player2
       if player1 == player2:
           messages.error(self.request, "Player 1 and Player 2 must be different!")
           return self.form_invalid(form)

       # Non-staff validation
       if not user.is_staff:
           try:
               user_player = user.player
               if player1 != user_player:
                   messages.error(self.request, "You must be Player 1!")
                   return self.form_invalid(form)
           except Player.DoesNotExist:
               messages.error(self.request, "No player profile!")
               return self.form_invalid(form)

       # Create Team objects
       from .models import Team

       if is_double:
           # Create 2-player teams
           team1 = Team.objects.create(name=f"{player1.name} & {player3.name}")
           team1.players.add(player1, player3)

           team2 = Team.objects.create(name=f"{player2.name} & {player4.name}")
           team2.players.add(player2, player4)
       else:
           # Create 1-player teams
           team1 = Team.objects.create(name=player1.name)
           team1.players.add(player1)

           team2 = Team.objects.create(name=player2.name)
           team2.players.add(player2)

       # Assign teams to match instance (don't save yet)
       form.instance.team1 = team1
       form.instance.team2 = team2
       form.instance.is_double = is_double

       messages.success(self.request, "Match created successfully!")
       return super().form_valid(form)  # This saves the match
   ```

2. **Fix bitwise OR on line 384:** Change `|` to `or`

3. **Consider:** The view's `get_form()` method is already correct - it locks player1 for non-staff and limits querysets. Keep this logic.

---

### 3. **views.py - ScheduledMatchCreateView Missing Team Creation** ⚠️ BLOCKING

**Location:** `ttstats/pingpong/views.py:894-993`

**Current State Analysis:**
- Lines 922-927: `get_form()` correctly locks player1 for non-staff ✓
- Lines 930: Correctly limits player2 queryset ✓
- Lines 945-946: Correctly extracts player1/player2 from `cleaned_data` ✓

**Issues:**
- **MISSING:** Team creation logic (same as MatchCreateView)
- **MISSING:** Assignment of teams to `scheduled_match.team1` and `scheduled_match.team2`
- Lines 977-978: Tries to send emails using player1/player2 variables (correct, but scheduled_match won't have team1/team2 set)

**Action Required:**

1. **Add team creation in form_valid() before line 973** (before `form.save()`):
   ```python
   def form_valid(self, form):
       user = self.request.user
       player1 = form.cleaned_data["player1"]
       player2 = form.cleaned_data["player2"]

       # Validation
       if player1 == player2:
           messages.error(self.request, "Players must be different!")
           return self.form_invalid(form)

       # Non-staff validation
       if not user.is_staff:
           try:
               user_player = user.player
               if player1 != user_player:
                   messages.error(self.request, "You must be Player 1!")
                   return self.form_invalid(form)
               form.instance.created_by = user_player
           except Player.DoesNotExist:
               messages.error(self.request, "No player profile!")
               return self.form_invalid(form)

       # Create 1-player teams (scheduled matches are singles only)
       from .models import Team
       team1 = Team.objects.create(name=player1.name)
       team1.players.add(player1)

       team2 = Team.objects.create(name=player2.name)
       team2.players.add(player2)

       # Assign teams to scheduled match
       form.instance.team1 = team1
       form.instance.team2 = team2

       # Save the scheduled match
       self.object = form.save()
       scheduled_match = self.object

       # Send emails (keep existing logic)
       send_scheduled_match_email(scheduled_match, player1)
       send_scheduled_match_email(scheduled_match, player2)

       # ... rest of method
   ```

---

### 4. **managers.py - MatchManager Uses Deleted Fields** ⚠️ BLOCKING

**Location:** `ttstats/pingpong/managers.py:5-38`

**Issues:**
- Lines 33-34: `visible_to()` filters by `Q(player1=user_player) | Q(player2=user_player)` but these fields were removed in migration 0010

**Why It Breaks:**
- `FieldError: Cannot resolve keyword 'player1' into field`
- All match queries for non-staff users will fail
- Dashboard, leaderboard, and match list views will crash

**Action Required:**
- Update filter to use team relationships:
  ```python
  Q(team1__players=user_player) | Q(team2__players=user_player)
  ```

---

### 5. **templates - Add JavaScript for Dynamic Dropdowns** ⚠️ HIGH PRIORITY

**Location:** `ttstats/pingpong/templates/pingpong/match_form.html` (and `scheduled_match_form.html`)

**Current State:** Static dropdowns show all players (except current user for non-staff)

**Required Behavior:**
- When user selects Player 2, Player 3 and Player 4 dropdowns should exclude Player 2
- When user selects Player 3, Player 4 dropdown should exclude Player 2 and Player 3
- When user changes selections, dropdowns should update immediately
- For non-staff users, Player 1 is locked (already implemented)

**Action Required:**

Add JavaScript to template (after form):

```html
<script>
document.addEventListener('DOMContentLoaded', function() {
    const player1Select = document.querySelector('[name="player1"]');
    const player2Select = document.querySelector('[name="player2"]');
    const player3Select = document.querySelector('[name="player3"]');
    const player4Select = document.querySelector('[name="player4"]');

    // Store original options
    const allPlayers = {
        player2: Array.from(player2Select.options).map(opt => ({value: opt.value, text: opt.text})),
        player3: Array.from(player3Select.options).map(opt => ({value: opt.value, text: opt.text})),
        player4: Array.from(player4Select.options).map(opt => ({value: opt.value, text: opt.text}))
    };

    function updateDropdowns() {
        const selectedPlayers = new Set([
            player1Select.value,
            player2Select.value,
            player3Select?.value,
            player4Select?.value
        ].filter(v => v));  // Remove empty values

        // Update player2 (exclude player1)
        updateDropdown(player2Select, allPlayers.player2, new Set([player1Select.value]));

        // Update player3 (exclude player1, player2)
        if (player3Select) {
            updateDropdown(player3Select, allPlayers.player3, new Set([player1Select.value, player2Select.value]));
        }

        // Update player4 (exclude player1, player2, player3)
        if (player4Select) {
            updateDropdown(player4Select, allPlayers.player4, new Set([player1Select.value, player2Select.value, player3Select?.value].filter(v => v)));
        }
    }

    function updateDropdown(selectEl, allOptions, excludeValues) {
        const currentValue = selectEl.value;
        selectEl.innerHTML = '<option value="">---------</option>';

        allOptions.forEach(opt => {
            if (!excludeValues.has(opt.value) || opt.value === currentValue) {
                const option = document.createElement('option');
                option.value = opt.value;
                option.text = opt.text;
                if (opt.value === currentValue) {
                    option.selected = true;
                }
                selectEl.appendChild(option);
            }
        });
    }

    // Attach event listeners
    if (player1Select) player1Select.addEventListener('change', updateDropdowns);
    if (player2Select) player2Select.addEventListener('change', updateDropdowns);
    if (player3Select) player3Select.addEventListener('change', updateDropdowns);
    if (player4Select) player4Select.addEventListener('change', updateDropdowns);

    // Initial update
    updateDropdowns();
});
</script>
```

**For ScheduledMatchForm:** Similar logic but only for player2 (exclude player1)

---

## Non-Critical Issues

### 6. **models.py - Typo in Related Name** 🐛 BUG

**Location:** `ttstats/pingpong/models.py:349`

**Issue:** `ScheduledMatch.team2` has `related_name="scheduled_matches_as_team22"` (double "2")

**Action Required:**
- Fix to `scheduled_matches_as_team2`
- Create migration

---

### 7. **views.py - MatchDetailView Incorrect Player Check** ⚠️ LOGIC ERROR

**Location:** `ttstats/pingpong/views.py:73`

**Issue:** `elif change.player == match.team2.players.all():` compares Player to QuerySet

**Action Required:** Change to `elif change.player in match.team2.players.all():`

---

### 8. **views.py - PlayerDetailView Winner Filter** ⚠️ LOGIC ERROR

**Location:** `ttstats/pingpong/views.py:131`

**Issue:** `match.winner.filter(players=self.object).exists()` - winner is ForeignKey, not QuerySet

**Action Required:** Change to `self.object in match.winner.players.all()`

---

### 9. **views.py - LeaderboardView Data Structure** ⚠️ LOGIC ERROR

**Location:** `ttstats/pingpong/views.py:486-492`

**Issue:** Tries to append to QuerySet

**Action Required:** Convert to list or remove append logic

---

### 10. **views.py - HeadToHeadStatsView Unsafe .first()** ⚠️ POTENTIAL ERROR

**Location:** `ttstats/pingpong/views.py:570, 647, 649, 656, 660`

**Issue:** No null checks for `.first()` calls

**Action Required:** Add null checks or data integrity constraints

---

### 11. **emails.py - Team Name Display** ⚠️ MINOR

**Location:** `ttstats/pingpong/emails.py:156`

**Issue:** Direct `.name` access instead of `__str__()` method

**Action Required:** Change to `str(scheduled_match.team2)`

---

### 12. **views.py - GameCreateView Grammar** ⚠️ UX ISSUE

**Location:** `ttstats/pingpong/views.py:273`

**Issue:** "Alice and Bob wins" should be "win"

**Action Required:** Implement smart conjugation based on `is_double`

---

## Proposed Action Plan

### Phase 1: Fix Critical Form Issues (BLOCKING)
**Estimated Time:** 2-3 hours

1. **Update MatchForm** (forms.py):
   - Declare player1, player2, player3, player4 as explicit ModelChoiceField instances
   - Remove team1, team2 from Meta.fields
   - Remove player1-4 from widgets dict (now in field definitions)
   - Fix is_double widget (Select, not ChoiceField)
   - Verify clean() method works with declared fields

2. **Update ScheduledMatchForm** (forms.py):
   - Declare player1, player2 as explicit ModelChoiceField instances
   - Remove team1, team2 from Meta.fields
   - Remove player1-2 from widgets dict

### Phase 2: Fix Critical View Issues (BLOCKING)
**Estimated Time:** 3-4 hours

3. **Add team creation to MatchCreateView.form_valid()**:
   - Extract player selections from cleaned_data
   - Create Team objects (1-player for singles, 2-player for doubles)
   - Assign teams to form.instance.team1 and form.instance.team2
   - Fix bitwise OR to logical OR

4. **Add team creation to ScheduledMatchCreateView.form_valid()**:
   - Create 1-player teams from player1/player2
   - Assign to form.instance
   - Keep email logic

5. **Fix MatchManager.visible_to()** (managers.py):
   - Change player1/player2 filter to team1__players/team2__players

### Phase 3: Add Dynamic Dropdown JavaScript (HIGH PRIORITY)
**Estimated Time:** 2 hours

6. **Add JavaScript to match_form.html**:
   - Implement dynamic dropdown filtering
   - Test with singles and doubles toggle
   - Test with staff and non-staff users

7. **Add JavaScript to scheduled_match_form.html**:
   - Simpler version (only player2 excludes player1)

### Phase 4: Fix Non-Critical Issues (MEDIUM PRIORITY)
**Estimated Time:** 2-3 hours

8. Fix model typo (related_name)
9. Fix MatchDetailView player check
10. Fix PlayerDetailView winner filter
11. Fix LeaderboardView data structure
12. Add null checks to HeadToHeadStatsView
13. Fix email team name display
14. Implement smart grammar for winner messages

### Phase 5: Testing & Verification (CRITICAL)
**Estimated Time:** 3-4 hours

15. **Manual Testing:**
    - Staff creates singles match (select any 2 players)
    - Staff creates doubles match (select any 4 players)
    - Non-staff creates singles match (locked as player1, select player2)
    - Non-staff creates doubles match (locked as player1, select 3 others)
    - Verify dropdowns update dynamically
    - Schedule matches (singles only)
    - View match details, player profiles, leaderboard
    - Verify emails sent correctly

16. **Data Verification:**
    - Check all matches have team1/team2
    - Check no orphaned teams
    - Run `python manage.py check`

---

## Execution Order

**CRITICAL PATH:**
1. Phase 1: Fix forms (items 1-2)
2. Phase 2: Fix views (items 3-5)
3. Phase 5: Basic testing (item 15)

**HIGH PRIORITY:**
4. Phase 3: Add JavaScript (items 6-7)
5. Phase 5: Full testing (items 15-16)

**MEDIUM PRIORITY:**
6. Phase 4: Fix non-critical issues (items 8-14)

---

## Risk Assessment

**HIGH RISK:**
- Forms and views currently broken for all match creation
- Non-staff users completely locked out (manager filter fails)
- App is non-functional without Phase 1-2 fixes

**MEDIUM RISK:**
- Without JavaScript, users can select duplicate players (bad UX)
- Logic errors will cause crashes in specific views

**LOW RISK:**
- Minor bugs and UX issues

**RECOMMENDATION:** Fix Phase 1-2 immediately. App is currently broken. Add Phase 3 before merging. Phase 4 can be done in follow-up PR.

---

## Notes

1. **Team Lifecycle:** Teams are ephemeral - created per match, not reusable
2. **Backwards Compatibility:** Migrations preserve historical 1v1 data
3. **Non-Staff UX:** Player 1 is always locked to current user (already implemented in views)
4. **Dynamic Dropdowns:** JavaScript required for real-time filtering (not Django form validation)
5. **Signal Behavior:** Verify signals handle both 1v1 and 2v2 correctly

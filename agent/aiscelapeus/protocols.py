"""Seed corpus of field first-aid protocols indexed into Moss.

Every entry is a single retrievable instruction block, written the way a
responder needs to hear it: short, imperative, one action per line. Text is
paraphrased from public first-aid guidance (AHA BLS, ERC, Stop the Bleed,
Red Cross field guides) and is NOT a substitute for clinical training.

`severity` is the criticality level (1-5, PRD 5.3) at which this protocol
becomes relevant, and is used for metadata filtering so a Level 2 laceration
never retrieves cardiac-arrest instructions.
"""

from __future__ import annotations

from typing import Any

PROTOCOLS: list[dict[str, Any]] = [
    {
        "id": "bls-adult-cpr",
        "category": "cardiac",
        "severity": 5,
        "title": "Adult CPR - unresponsive, not breathing normally",
        "text": (
            "Unresponsive on the ground and not breathing normally. The casualty went down "
            "without warning on dry land and does not react at all to shouting or shaking. "
            # "The heart has stopped beating" was here and was deleted. It is an
            # assertion about what is happening inside the chest, which the
            # caller cannot observe and which the scene can appear to CONTRADICT
            # - a caller who felt a flutter or saw the chest move now has grounds
            # to decide the protocol does not apply, and the instinct on hearing
            # it is to go and verify, which is the ~coin-flip lay pulse check
            # ILCOR removed. Recognition text states what is OBSERVABLE only.
            # Nothing is lost: "begin chest compressions immediately" already
            # follows, with no precondition.
            "Breathing is absent, or only occasional gasping or snoring gurgles, which is "
            "not normal breathing. Begin chest compressions immediately. "
            "Adult CPR. Confirm unresponsive and not breathing normally. Call for help and "
            "start compressions now. Heel of one hand on the centre of the chest, other hand "
            "on top, arms straight. Push hard, at least 5 centimetres deep. Push fast, 100 to "
            "120 compressions per minute. Let the chest come all the way back up between "
            "compressions. Thirty compressions, then two rescue breaths if trained. Do not "
            "stop for more than 10 seconds. Swap rescuers every 2 minutes to stay effective."
        ),
    },
    {
        "id": "bls-aed",
        "category": "cardiac",
        "severity": 5,
        "title": "AED use during cardiac arrest",
        "text": (
            "AED. As soon as the defibrillator arrives, switch it on and follow the voice "
            "prompts. Bare the chest and dry it. One pad below the right collarbone, one pad "
            "on the left side below the armpit. Stop compressions only while it analyses. "
            "Make sure nobody is touching the patient before the shock. Resume compressions "
            "immediately after the shock without waiting to check a pulse."
        ),
    },
    {
        "id": "bleed-tourniquet",
        "category": "haemorrhage",
        "severity": 4,
        "title": "Life-threatening limb bleeding - tourniquet",
        "text": (
            "Severe limb bleeding. If blood is spurting, pooling, or soaking through, apply a "
            "tourniquet. Place it 5 to 8 centimetres above the wound, never over a joint. "
            "Tighten until the bleeding stops, then secure the windlass. Write the time it "
            "was applied. It will hurt; that is expected. Do not loosen or remove it. If "
            "bleeding continues, apply a second tourniquet just above the first."
        ),
    },
    {
        "id": "bleed-pressure-packing",
        "category": "haemorrhage",
        "severity": 3,
        "title": "Wound packing and direct pressure",
        "text": (
            "Heavy bleeding that will not stop, anywhere a tourniquet cannot be used - the "
            "neck, the shoulder hollow, the armpit, where the thigh joins the trunk, or any "
            "wound when no tourniquet is available. Blood is welling up and reappearing as "
            "fast as it is mopped away, soaking through dressings and clothing, pooling "
            "underneath. Squeezing the surface has already failed. This is serious blood "
            "loss, not a graze needing a plaster. "
            "Direct pressure and packing. For bleeding on the neck, armpit, or groin where a "
            "tourniquet cannot go, pack the wound. Push gauze or clean cloth deep into the "
            "wound, right down onto the bleeding vessel, then hold firm pressure with both "
            "hands for at least 3 minutes without lifting to check. Once bleeding slows, "
            "bandage tightly over the packing and keep pressure on."
        ),
    },
    {
        "id": "airway-choking-adult",
        "category": "airway",
        "severity": 4,
        "title": "Choking - conscious adult",
        "text": (
            # "They are alert and aware of you throughout" was here and was
            # deleted: it is clinically false, it contradicts this document's own
            # closing line ("if they go unconscious ... start CPR"), and as LLM
            # grounding it turned a presentation cue into an ELIGIBILITY GATE.
            # This is the only choking document in the corpus, so a caller
            # describing a casualty who has already gone limp matched no cue here
            # and the agent would coach back blows instead of compressions.
            # Consciousness is now the state ON ARRIVAL with an explicit
            # transition, which keeps the ("conscious", "standing") discriminator
            # the retrieval gate needs without claiming it persists.
            # Wording is constrained from BOTH sides here, and the first attempt
            # failed the retrieval side. Clinically the transition out of this
            # protocol must be stated (see below). But phrasing it as "...becomes
            # CPR" put CPR vocabulary in this document, and measurement showed it
            # then won the *cardiac arrest* query at rank 1 while real choking
            # fell behind anaphylaxis - critical top-1 dropped 8/8 -> 6/8. A
            # second magnet document, exactly the drowning-rescue failure mode.
            #
            # So the transition is carried by the pre-existing closing line of
            # the instruction block ("If they go unconscious, lower them down and
            # start CPR"), which is where a responder needs it, and the
            # recognition text stays on what distinguishes choking: a conscious
            # person with a mechanical blockage they are trying to clear.
            "Conscious right now, standing or sitting, gripping their own neck, unable to "
            "speak or cough properly, usually while eating. A solid obstruction in the "
            "throat, to be expelled by force. This protocol applies only while they are "
            "still responsive. "
            "Choking adult, still conscious. If they can cough, encourage coughing. If the "
            "cough is silent or they cannot breathe, give 5 sharp back blows between the "
            "shoulder blades with the heel of your hand, leaning them forward. If that fails, "
            "give 5 abdominal thrusts: fist above the navel, other hand over it, pull sharply "
            "inward and upward. Alternate 5 and 5. If they go unconscious, lower them down "
            "and start CPR."
        ),
    },
    {
        "id": "airway-recovery-position",
        "category": "airway",
        "severity": 3,
        "title": "Unconscious but breathing - recovery position",
        "text": (
            "Unconscious but breathing normally. Put them in the recovery position. Kneel "
            "beside them, place the near arm out at a right angle, bring the far hand against "
            "their cheek, pull the far knee up and roll them toward you onto their side. Tilt "
            "the head back to keep the airway open. Recheck breathing every minute. Do not "
            "roll them if you suspect a spinal injury unless the airway is threatened."
        ),
    },
    {
        "id": "drowning-rescue",
        "category": "drowning",
        "severity": 5,
        "title": "Drowning - out of water, unresponsive",
        "text": (
            "Submersion incident. The casualty has just been dragged out of water - a "
            "swimming pool, bath, pond, river, canal or the sea - after going under and "
            "staying under. They are soaking wet, limp and lifeless. This arrest was caused "
            "by lack of oxygen while submerged, which is why the sequence below differs from "
            "a dry-land cardiac collapse: breaths come first. "
            "Drowning. Once out of the water, check breathing. Drowning is a hypoxic arrest, "
            "so start with 5 rescue breaths before compressions, then 30 compressions to 2 "
            "breaths. Expect vomiting; turn the head to the side and clear the mouth, then "
            "continue. Do not waste time trying to drain water from the lungs. Get them warm "
            "and dry once breathing returns."
        ),
    },
    {
        "id": "shock-management",
        "category": "circulation",
        "severity": 4,
        "title": "Hypovolaemic shock",
        "text": (
            "The circulation is collapsing from lost volume after a major injury, heavy "
            "blood loss, extensive burns, or relentless vomiting. The casualty is still "
            "rousable, but colour has drained from the face to grey or ashen, the surface "
            "feels chilled and slick with sweat, the breathing has gone fast and shallow, "
            "and they are thirsty, agitated and incoherent. The cause is lost volume, not "
            "exposure, so blankets alone will not correct it. "
            "Shock. Pale, cold, clammy skin, fast weak pulse, confusion or drowsiness. Lay "
            "them flat and raise the legs about 30 centimetres unless a leg or spine is "
            "injured. Stop any ongoing bleeding first. Keep them warm with a blanket or coat, "
            "including underneath them. Give nothing to eat or drink. Recheck breathing and "
            "responsiveness every minute."
        ),
    },
    {
        "id": "burns-thermal",
        "category": "burns",
        "severity": 3,
        "title": "Thermal burn",
        "text": (
            "Burns. Cool the burn under cool running water for 20 minutes. Do not use ice, "
            "butter, or ointment. Remove rings, watches, and tight clothing near the burn "
            "before swelling starts, but leave anything stuck to the skin. Cover loosely with "
            "cling film or a clean non-fluffy dressing. Keep the rest of the body warm. Any "
            "burn to the face, hands, feet, genitals, or any burn larger than the patient's "
            "palm needs a hospital."
        ),
    },
    {
        "id": "fracture-immobilise",
        "category": "trauma",
        "severity": 2,
        "title": "Suspected fracture",
        "text": (
            "Suspected fracture. Do not try to straighten the limb. Support it in the "
            "position found, using padding and a splint that spans the joints above and "
            "below. Check fingers or toes beyond the injury for warmth, colour, and "
            "sensation before and after splinting. Apply a cold pack over cloth for 20 "
            "minutes. If bone is through the skin, cover with a sterile dressing and do not "
            "push it back."
        ),
    },
    {
        "id": "spinal-precaution",
        "category": "trauma",
        "severity": 4,
        "title": "Suspected spinal injury",
        "text": (
            "Suspected spinal injury after a fall, dive, or high-speed impact. Tell them not "
            "to move. Kneel behind the head and hold it steady in line with the body with "
            "both hands. Do not move them unless they are in immediate danger or you need "
            "the airway. If you must move them, keep the head, neck, and spine in one line "
            "and use several rescuers to log-roll together."
        ),
    },
    {
        "id": "anaphylaxis",
        "category": "allergy",
        "severity": 5,
        "title": "Anaphylaxis",
        "text": (
            "A severe allergic reaction is escalating. Within minutes of an allergen - a "
            "trigger food, an insect sting, or a new medicine - the tongue and airway lining "
            "swell and tighten from inside. This is an allergic reaction spreading through "
            "the whole body rather than a blockage: the voice turns croaky, each breath squeaks "
            "or wheezes, an itchy blotchy hives rash spreads over the skin, and there may be "
            "retching or sudden collapse. Ask for an adrenaline auto-injector and check "
            "pockets and bags; if there is none anywhere, this still needs an ambulance now. "
            "Anaphylaxis. Swelling of the lips or tongue, hoarse voice, wheeze, widespread "
            "rash, or sudden collapse after an exposure. Use their adrenaline auto-injector "
            "immediately: firmly into the outer thigh, hold for the time printed on the "
            "device, and note the time. Lay them flat with legs raised; sit them up only if "
            "breathing is hard. If there is no improvement in 5 minutes, give a second dose "
            "in the other thigh. This always needs emergency transport, even if they recover."
        ),
    },
    {
        "id": "seizure",
        "category": "neuro",
        "severity": 3,
        "title": "Active seizure",
        "text": (
            "A convulsion is under way. The casualty is on the ground, whole body rigid then "
            "jerking in rhythm, unaware of you, jaw clenched, perhaps froth at the lips, and "
            "may pass urine. The movement is the illness itself, not a struggle to escape "
            "anything. It typically subsides unaided within a couple of minutes, "
            "and no amount of gripping or pinning shortens it; restraining causes injury. "
            "Seizure. Do not restrain them and do not put anything in their mouth. Clear hard "
            "objects away and cushion the head. Note the start time. Once the jerking stops, "
            "roll them into the recovery position and check breathing. Call for emergency "
            "help if the seizure lasts more than 5 minutes, repeats without recovery, or if "
            "this is their first seizure."
        ),
    },
    {
        "id": "stroke-fast",
        "category": "neuro",
        "severity": 4,
        "title": "Suspected stroke - FAST",
        "text": (
            "Suspected stroke. Check FAST. Face: ask them to smile, look for one side "
            "drooping. Arms: ask them to raise both arms, look for one drifting down. Speech: "
            "ask them to repeat a sentence, listen for slurring. Time: note when they were "
            "last seen well, because treatment depends on it. Keep them still and "
            "comfortable, nothing to eat or drink, and get emergency transport now."
        ),
    },
    {
        "id": "hypoglycaemia",
        "category": "metabolic",
        "severity": 3,
        "title": "Low blood sugar",
        "text": (
            "Low blood sugar. Shaky, sweating, confused, aggressive, or drowsy in someone "
            "known to have diabetes. If they are awake and can swallow, give 15 to 20 grams "
            "of fast sugar: glucose tablets, juice, or sugary drink, not diet. Recheck after "
            "10 minutes and repeat once if still unwell, then give a longer-acting snack. If "
            "they are unconscious or cannot swallow, give nothing by mouth, place them in the "
            "recovery position, and get emergency help."
        ),
    },
    {
        "id": "minor-wound",
        "category": "wounds",
        "severity": 1,
        "title": "Minor cut or graze",
        "text": (
            "Minor cut or graze. Wash your hands, then rinse the wound under clean running "
            "water to flush out dirt. Pat dry around it and cover with a sterile adhesive "
            "dressing. Apply gentle pressure for a few minutes if it is still oozing. Check "
            "tetanus cover if the wound is dirty or from a puncture. Seek care if the edges "
            "gape, it will not stop bleeding, or it later becomes red, hot, or swollen."
        ),
    },
    {
        "id": "heat-stroke",
        "category": "environmental",
        "severity": 4,
        "title": "Heat stroke",
        "text": (
            "Overheated from within after prolonged heat exposure or heavy exertion. The "
            "skin is intact and unmarked - no flame, no scalding liquid, no blistering, no "
            "wound to dress. What is dangerous is the core temperature, and the brain is "
            "affected too: muddled, slurring, incoherent, staggering or already down. Cool "
            "the whole casualty, not a body part. "
            "Heat stroke. Hot skin, confusion or collapse after heat or exertion. This is an "
            "emergency. Move them into shade, strip outer clothing, and cool aggressively: "
            "cold water immersion if possible, otherwise soak them and fan hard, with cold "
            "packs to the neck, armpits, and groin. Keep cooling until they are alert or help "
            "arrives. Do not give fever medication."
        ),
    },
    {
        "id": "hypothermia",
        "category": "environmental",
        "severity": 4,
        "title": "Hypothermia",
        "text": (
            "Hypothermia. Shivering, slurred speech, clumsiness, drowsiness after cold "
            "exposure. Move them out of the cold and remove wet clothing by cutting it off "
            "rather than making them move. Wrap in dry layers including the head, and "
            "insulate them from the ground. Handle them gently; rough movement can trigger a "
            "dangerous heart rhythm. Warm sweet drinks only if fully alert. No alcohol, no "
            "rubbing the limbs."
        ),
    },
    {
        "id": "chest-pain-cardiac",
        "category": "cardiac",
        "severity": 4,
        "title": "Chest pain, suspected heart attack",
        "text": (
            "The casualty is awake, talking, and complaining of pain. They are conscious and "
            "breathing, seated or standing, frightened but responsive - a heart attack in "
            "progress rather than an arrest, so the priority is keeping them still and "
            "calm while help comes. "
            "Suspected heart attack. Crushing central chest pain, possibly spreading to the "
            "arm, jaw, or back, with sweating, nausea, or breathlessness. Sit them down, "
            "leaning back with knees bent, and keep them calm and still. Call emergency "
            "services now. If they are not allergic and have no bleeding disorder, a 300 "
            "milligram aspirin chewed slowly can help. Be ready to start CPR if they collapse."
        ),
    },
    {
        "id": "scene-safety",
        "category": "general",
        "severity": 1,
        "title": "Scene safety and primary survey",
        "text": (
            "Before you touch anyone, check the scene is safe for you: traffic, fire, "
            "electricity, gas, water, unstable structures, aggression. You cannot help if you "
            "become the second casualty. Then run the primary survey in order: Danger, "
            "Response, Airway, Breathing, Circulation. Fix each problem as you find it before "
            "moving to the next."
        ),
    },
]


def as_documents() -> list[dict[str, Any]]:
    """Shape the corpus into Moss documents with filterable metadata."""
    docs: list[dict[str, Any]] = []
    for entry in PROTOCOLS:
        docs.append(
            {
                "id": entry["id"],
                "text": f"{entry['title']}. {entry['text']}",
                "metadata": {
                    "category": entry["category"],
                    "severity": entry["severity"],
                    "title": entry["title"],
                    "source": "field-first-aid-corpus",
                },
            }
        )
    return docs

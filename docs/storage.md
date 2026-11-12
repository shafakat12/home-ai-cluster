# Storage: drives, folders and RAID

Models are big (3–30 GB each), so you choose where they live. Your options:

| Option | Erases? | Good for |
|---|---|---|
| **System disk** (`/opt/ai/models`, the default) | no | one-disk servers, getting started |
| **A folder** on a drive that is already set up | no | a data drive you already use (for example mounted at `/mnt/data`) |
| **One whole drive** | **yes** | a spare HDD or SSD just for the AI |
| **RAID**: several drives combined into one | **yes** | more space, more speed, or survives a dead drive |

The **installer asks** the first time it runs. If the server has only one disk, it says so and skips the question. You can change your choice **any time later**:

* in the control panel: **Storage** tab (`http://SERVER-IP:8091`), or
* on the server: `sudo ai-storage`

## In the control panel

The **Storage** tab shows where models and backups are now, every drive with its state, and any RAID with its health and rebuild progress.

* **Use drives:** tick one drive ("One drive") or several and pick a RAID level. The page shows the usable space and exactly which drives will be erased. **Show what will happen** is a preview that changes nothing. To start, type `ERASE`, enter your admin password and press **Erase and set up**.
* **Use a folder:** type a path and press **Move the models there** (or **Move the backups there**). Nothing is erased.
* **Progress** shows the live log. The AI pauses while models are copied and starts again by itself. You can close the page; the job keeps running on the server.

Models already downloaded are **moved** to the new place automatically: the AI pauses while they copy, the model paths are updated, and the old copy is removed after the copy is checked. The nightly backups can move along with them.

## The menu

```
  Home AI storage - where the models and backups are kept
  Models:  /opt/ai/models  (drive /dev/sda2, mounted at /, 80G free)
  Backups: /opt/ai/backups  (drive /dev/sda2, mounted at /, 80G free)

  1) Keep it as it is
  2) Use a folder on a drive that is already set up (erases nothing)
  3) Use one whole drive (ERASES it)
  4) Combine several drives into a RAID (ERASES them)
  5) Move only the nightly backups
  6) Show drives and RAID status
```

The drive list shows what each drive is:

```
  #   Drive          Size     Type  Model                      State
  1   /dev/sda       119.2G   SSD   KINGSTON SA400S37120G      system disk (Ubuntu is on it) - cannot be used
  2   /dev/sdb       1.8T     HDD   ST2000DM008-2UB102         empty - ready to use
  3   /dev/sdc       931.5G   HDD   WDC WD10SPZX-00Z10T0       has data (ntfs Backup) - will be ERASED if chosen
```

**Safety:** the system disk, drives that are mounted, and drives that already belong to a RAID, LVM or encrypted volume are **never** offered for erasing. Before erasing anything you see exactly which drives will be wiped, and you must type `ERASE`. It also checks first that the new storage is big enough for the models you already have.

## Which RAID?

| Level | Drives | Usable space | A drive dies… | Notes |
|---|---|---|---|---|
| **RAID 0** | 2+ | all of them | **everything on the RAID is lost** | fastest. OK for models (they can be downloaded again), not for backups |
| **RAID 1** | 2+ | one drive | keeps working | mirror: every drive holds the same copy. Simplest safe choice |
| **RAID 5** | 3+ | all but one | keeps working | good balance of space and safety |
| **RAID 10** | 4+ | half | keeps working | fast and safe |

* Drives of different sizes work, but every drive is only used up to the size of the **smallest** one.
* Mixing HDD and SSD works, but the RAID runs at the speed of the slowest drive.
* RAID 1/5/10 build their safety copy in the background after they are created (hours for big drives). You can use them right away; they are a bit slower until that finishes. Progress is shown in the control panel's **Health** tab.
* **RAID is not a backup.** A deleted or damaged file is gone from every drive at once. The nightly backups cover settings and chats. Models can always be downloaded again.
* It uses standard Linux software RAID (`mdadm`). No special hardware or BIOS RAID setting is needed. Leave BIOS RAID **off**.

## Without the menu (scripts, unattended installs)

```bash
sudo ai-storage list                                   # drives + current storage + RAID status
sudo ai-storage folder /mnt/data/ai-models             # use a folder (erases nothing)
sudo ai-storage drive /dev/sdb --backups               # erase /dev/sdb and use it (models + backups)
sudo ai-storage raid 1 /dev/sdb /dev/sdc --backups     # erase both, mirror them
sudo ai-storage raid 5 /dev/sdb /dev/sdc /dev/sdd
sudo ai-storage backups /mnt/data/ai-backups           # move only the backups
sudo ai-storage drive /dev/sdb --dry-run               # show what would happen, change nothing
```

Or in `config.env` before installing:

```bash
STORAGE=raid1:/dev/disk/by-id/ata-ST2000DM008_SERIAL1,/dev/disk/by-id/ata-ST2000DM008_SERIAL2
STORAGE_ERASE_OK=yes        # only needed if nobody types ERASE (unattended install)
STORAGE_BACKUPS=yes
```

`/dev/sdX` names can change when you add or remove drives. `/dev/disk/by-id/...` names never do (list them with `ls -l /dev/disk/by-id/`). A `STORAGE=` setting is applied **once**; re-running the installer does not erase anything again.

## What it does exactly

* **Drive:** a new GPT partition table with one partition, formatted **ext4** with the label `ai-storage`.
* **RAID:** one partition per drive, then `mdadm --create /dev/md/ai-storage`, formatted ext4. The array is saved in `/etc/mdadm/mdadm.conf` and the boot files are updated so it starts on every boot.
* Mounted at **`/mnt/ai-storage`** (a second one at `/mnt/ai-storage2`, …), added to `/etc/fstab` by its UUID with `nofail`, so the server still boots if the drive is missing. Your old fstab is saved as `/etc/fstab.before-ai-storage.<date>`.
* Models go to `/mnt/ai-storage/models`, backups (if chosen) to `/mnt/ai-storage/backups`. The paths are stored in `/opt/ai/ai.conf` (`MODELS_DIR`, `BACKUP_DIR`).
* A **folder** that already has other files gets a sub-folder `ai-models` (or `ai-backups`), so the AI's files never mix with yours.
* Windows-format (NTFS/exFAT), network, or not-in-fstab drives work but you get a warning first. They are slower, or may be missing after a restart.

## When a RAID drive fails

The Health tab shows **RAID … PROBLEM** and, if Telegram is set up, you get a message (checked every 10 minutes). RAID 1/5/10 keep working with one dead drive. Replace it soon:

```bash
cat /proc/mdstat                                   # which array, which drive is missing or marked (F)
sudo mdadm --detail /dev/md/ai-storage
sudo mdadm /dev/md/ai-storage --remove failed      # take the failed drive out
# shut down, swap the drive, start again. Then copy the partition layout from a good drive (here sdb) to the new one (here sdc)
# (sgdisk is in the gdisk package: sudo apt install gdisk):
sudo sgdisk /dev/sdb -R /dev/sdc && sudo sgdisk -G /dev/sdc
sudo mdadm /dev/md/ai-storage --add /dev/sdc1      # rebuild starts; progress in the Health tab
```

Check the drive names carefully: `sgdisk -R` overwrites the partition table of the **second** drive you name.

## Removing it

`sudo bash uninstall.sh` leaves the storage mounted. To stop using a drive, first move the models back (`sudo ai-storage folder /opt/ai/models`). Then delete its `/mnt/ai-storage` line from `/etc/fstab` and, for a RAID, run `sudo mdadm --stop /dev/md/ai-storage` and delete its `ARRAY` line from `/etc/mdadm/mdadm.conf`.
